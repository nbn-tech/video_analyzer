"""サムネット調査用: 1回分の OCR・文字起こし・サムネ検出結果を Gemini に渡し、超町人ごとに1行の表を作る。

入力:
    probe_dir   samunet_probe.py の出力（ocr_rows.json / audio_rows.json）
    thumbs_dir  find_thumbnails.py の出力（thumbnails.json / thumb_XX.jpg）
    video       元の動画（名前テロップの画像を切り出して Gemini に読ませるため）
出力（out_dir）:
    rows.json   Gemini の結果（回の情報＋超町人ごとの行）
    rows.csv    1超町人＝1行の表（Excel で開ける UTF-8 BOM 付き）

- Gemini を1回呼ぶ（料金あり。59分の回で数十円程度）
- S3 には何も書かない
- ファイル名に "worker" / "backfill" を入れないこと（stop_worker.ps1 が名前で止めるため）

使い方:
    .venv\\Scripts\\python samunet\\samunet_summarize.py 4 D:\\chocosamu\\probe_out\\#04 D:\\chocosamu\\thumbs\\#04 D:\\chocosamu\\rows\\#04 "D:\\chocosamu\\#04.mp4"
"""

import csv
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 特徴の判定が flash では甘いため pro を使う（毎日の分析の設定 GEMINI_MODEL とは別）
MODEL = "gemini-2.5-pro"
PRICE_IN_USD_PER_1M = 1.25    # gemini-2.5-pro 入力（20万トークン以下）
PRICE_OUT_USD_PER_1M = 10.0   # gemini-2.5-pro 出力（思考を含む）

GENRE_RULES = """
■「特徴」（genre）の分類ルール
判定はサムネになっている人物（サムネに写り、サムネの文言が指している人物）を基準にする。
同じ行に家族・同行者がいても、サムネの人物に当てはまらない分類は付けない。サムネが無い行は、紹介の中心になった人物で判定する。

① スーパーキッズ: 一般の方のうち、スポーツや勉強に秀でた、乳幼児から大学生程度までの子どもが登場する場合
  ・今は大人でも、子ども時代（大学生程度まで）にスポーツや勉強ですごい結果を残したことが紹介の中心なら「スーパーキッズ」
  ・子どもを指導するコーチや親など、大人本人の実績が中心の場合は「スーパーキッズ」にしない
② アスリート: 大人になってからのスポーツでの実績が紹介の中心の場合（プロ選手・五輪や日本代表・実業団・全国レベルの指導者など。現役・引退は問わない）
  ・実績が子ども時代（大学生程度まで）のものなら「アスリート」ではなく「スーパーキッズ」
  ・子ども時代から大人になっても続けて活躍している場合は、紹介の中心がどちらの時期かで決め、両方が明確に紹介されていれば両方付ける
③ 職人: 伝統工芸・ものづくり・手仕事の技術を職業にしていることが紹介の中心の場合（陶芸、染物、着物、靴磨き、大工など）
  ・料理人は職人ではなく「フード」で扱う
④ マニア: テロップ・ナレーションなどで「〇〇が好きすぎる」と紹介されている場合。単に趣味や収集活動をしているだけでは分類しない
⑤ フード: 特徴的な飲食店が登場する場合。さらに、紹介された料理や店舗の特徴に応じて以下を追加する
  ・海鮮: 魚介類（うなぎなど川魚も含む）、刺身、寿司、海鮮丼、海鮮料理などが主に紹介される場合
  ・肉: 焼肉、ステーキ、ハンバーグ、肉料理などが主に紹介される場合
  ・麺: ラーメン、うどん、そば、パスタ、焼きそばなどが主に紹介される場合
  ・スイーツ: ケーキ、パフェ、プリン、和菓子、アイスクリーム、その他の菓子・デザートなどが主に紹介される場合
  ・中華: 餃子、麻婆豆腐、チャーハン、点心、中華料理などが主に紹介される場合
  ・デカ盛り: 料理の量やサイズが通常より著しく大きいことを、番組内で特徴として紹介している場合
  ・異国料理: イタリア、フランス、インド、タイ、ベトナム、韓国、トルコ料理など日本以外の料理が主に紹介される場合（中華は「中華」に分類し、ここには含めない）

共通ルール:
・複数の条件に当てはまる場合は、該当する分類名をすべて記載する
・「海鮮」「肉」「麺」「スイーツ」「中華」「デカ盛り」「異国料理」は「フード」の追加分類として扱い、必ず「フード」と併記する
・複数分類は「、」で区切る
・記載順は「スーパーキッズ、アスリート、職人、マニア、フード、海鮮、肉、麺、スイーツ、中華、デカ盛り、異国料理」
・どの条件にも当てはまらない場合は「該当なし」
・料理に魚介類や肉類が材料として含まれているだけでは「海鮮」「肉」に分類しない
・「デカ盛り」は見た目だけで判断せず、大盛り・巨大サイズ・大食い向けなどの特徴が明確に紹介されている場合に限る
・番組内で明示されていない情報から、分類を推測しない

判断の例（番組担当者が確認した正解）:
・飛行機をプロ仕様のカメラで撮る住職。「子どもの頃から飛行機が大好き」と紹介 →「マニア」
  （「大好き」「〇〇愛」「〇〇にこだわる」など、好きなものへの熱中ぶりが紹介の柱になっていればマニアに含める）
・50年代アメリカにこだわり、グッズを30年集めたアメリカンダイナーの店主 →「マニア、フード、異国料理」
  （アメリカ料理の店はハンバーガーなど肉料理が出ても「異国料理」を優先する）
・音楽に合わせて独特なダンスを披露した5歳の女の子 →「スーパーキッズ」
・17歳の専門学校生。小学6年生でチアダンスの世界大会に優勝 →「スーパーキッズ」
・焼き芋店を営む兄妹。サムネは妹（38歳）で、教え子を国体優勝に導いたソフトテニスのコーチ →「アスリート、フード、スイーツ」
  （コーチ本人は大人なのでスーパーキッズではない）
・産直市場の食堂を営む結婚60年の夫婦。紹介の中心は「別居で夫婦円満」だが、食堂と名物のどて串も映る →「フード」
  （超町人が飲食店を営んでいて、店や料理が番組内で紹介されていれば、紹介の主題が店以外でも「フード」を付ける）
・TVチャンピオンの金魚すくい選手権で3連覇した社長。息子たちも金魚すくいの大会に出ているが、サムネは社長本人 → スーパーキッズは付けない
・うなぎ店の大将 →「フード、海鮮」
・冷やしたぬきそばが「好きすぎる」と紹介され専門店を開いた店主。独学でアート活動もしている →「マニア、フード、麺」
  （「好きすぎる」と明示されたら必ずマニア。料理人や画家・アーティストは職人に含めない）
・魚が大好きで深海生物に「どハマり」している7歳の男の子 →「マニア」
  （スポーツや勉強の実績ではない趣味・好きなものは、子どもでもスーパーキッズにしない）
""".strip()

PROMPT = """
あなたはバラエティ番組「超町人！チョコレートサムネット」（以下サムネット）1回分の分析担当です。
入力として、この回の OCR（画面のテロップ）、Whisper の文字起こし（音声）、サムネ発表画面の表示区間が与えられます。
OCR と文字起こしには読み間違いが含まれます。同じテロップが何度も出るので、多数決で正しい表記を選んでください。
固有名詞（人名・店名・地名）は OCR を優先し、文字起こしは内容の理解に使ってください。

## 番組の構成
- 冒頭にスレート（タイトル＝ロケ地、放送日、放送時間）。スレートは動画の先頭とは限らない
- タレント（サムネハンター）の紹介カードのあと、街で一般の方（超町人）を探すロケ VTR
- 超町人ごとに、右上に小見出しテロップ（例「初セントレア 職業柄あまり遠出ができない」）が出て、下に「名前さん(年齢)」のテロップが出る
- VTR のあと、スタジオでその超町人のサムネが「金の額縁」に入って発表される（サムネ発表区間として与える）
- 最後に、その回の No.1 超町人（MVS / MVC など）が発表される
- 声をかけたが超町人にならなかった通行人もいる。小見出しが付かず短いやりとりだけの人は行にしない

## 作るもの
超町人ごとに1行。夫婦・家族・グループで1組として紹介された場合は1行にまとめ、名前を全員並べる。
サムネが無い超町人も行にする。サムネ発表区間のうち、超町人ではない演出（スタジオの企画など）は、どの行にも割り当てない。

各行の項目:
- start_sec / end_sec: その超町人のロケ VTR の開始・終了秒（スタジオでのサムネ発表は含めない）
- names: 名前の配列（テロップの表記。名前が出ない場合は空配列）
- ages: 年齢の配列（names と同じ順。分からなければ null）
- title: 肩書・小見出し（右上の小見出しテロップ。複数あれば代表的なもの）
- place: 取材場所（「〇〇で調査開始」「現在地」などのテロップ。分からなければ空文字）
- talents: その超町人を取材したタレントの name の配列（分かる範囲で。チームが分かれていなければ全員。書き方は episode.talents の name と同じ）
- summary: ロケ内容の概要（2〜3文、テロップとナレーションで明示された事実だけ）
- genre: 下の分類ルールに従った「特徴」
- genre_reason: genre の根拠になったテロップ・ナレーション（短く）
- thumbnail_index: その超町人のサムネ発表区間の index（無ければ null）
- thumbnail_copy: サムネ画像に書かれている文言（画像を添付する。画像の index と同じ番号のものを読む。無ければ空文字）
- is_top: その回の No.1 超町人として発表された場合 true

{genre_rules}

## 回の情報（episode）
- broadcast_date: 放送日（YYYY-MM-DD）。年が読めない場合は曜日と月日から決め、date_note にその旨を書く
- pref / city / area: ロケ地（スレートのタイトルや「今回の舞台は〜」から）を3つに分ける
  - pref: 都道府県名を「県」「府」「都」「道」まで付けて（例「愛知県」「岐阜県」）
  - city: 市区町村の正式名（例「常滑市」「名古屋市」「長久手市」）
  - area: 市区町村より細かい舞台名があれば（例「八事」「大須」。「エリア」「編」は付けない）。無ければ空文字
- talents: サムネハンター（街で取材したタレント）の全員。スタジオの MC（チョコレートプラネット）は含めない。1人ずつ {{"name", "group"}} で書く
  - name: 個人名の正式な芸名だけ。コンビ名・かっこ書きは付けない。姓と名の間に空白を入れない（例「村上健志」「酒井貴士」「井上裕介」「やす子」）
  - group: コンビ名・グループ名の正式表記（例「フルーツポンチ」「ザ・マミィ」「NON STYLE」）。ピンの芸人・タレントは空文字
- mc: スタジオの MC

## 注意
- place は、その超町人の VTR より前で最後に出た「〇〇で調査開始」「現在地」の場所、またはその超町人の店名。前後の超町人の場所と取り違えない
- 超町人の名前は、添付する「名前テロップ画像」（OCR で「〇〇さん(〇歳)」が読めた時刻の画面下半分）から読むことを最優先にする。OCR の名前は崩れていることが多い
- 名前テロップには小さくふりがなが付くことがある。ふりがなと漢字が食い違う・漢字が読み取りにくい場合は、画像で確かめたうえで、自信がなければ name_note にふりがなと迷った候補を書く

## 出力
JSON のみを返す（マークダウンの囲みは付けない）。
{{"episode": {{"broadcast_date": "...", "date_note": "", "pref": "...", "city": "...", "area": "", "talents": [{{"name": "...", "group": ""}}], "mc": ["..."]}},
  "rows": [{{"start_sec": 0, "end_sec": 0, "names": [], "ages": [], "title": "", "place": "", "talents": [],
             "summary": "", "genre": "", "genre_reason": "", "thumbnail_index": null, "thumbnail_copy": "", "is_top": false, "name_note": ""}}]}}
""".strip()


GENRE_ORDER = ["スーパーキッズ", "アスリート", "職人", "マニア", "フード", "海鮮", "肉", "麺", "スイーツ", "中華", "デカ盛り", "異国料理"]
FOOD_SUB = set(GENRE_ORDER[GENRE_ORDER.index("フード") + 1:])


def _normalize_genre(raw: str) -> str:
    """分類ルールの並び順と「フード併記」を機械的にそろえる。知らない分類名は落とす。"""
    names = {g.strip() for g in str(raw or "").replace(",", "、").split("、") if g.strip()}
    if names & FOOD_SUB:
        names.add("フード")
    ordered = [g for g in GENRE_ORDER if g in names]
    return "、".join(ordered) if ordered else "該当なし"


FIRST_YEAR = 2023  # #01 の放送年。年が読めないときはこの年以降で日曜になる年を探す


def _slate_date(ocr_rows: list[dict]) -> tuple[str, str]:
    """スレートの「放送日 26日」「2024年 10月」から放送日を決める。Gemini は月を取り違えることがあるため。

    年が欠けている（「年 10月」「202 年」）場合は、放送が日曜なので、日曜になる年を探す。
    戻り値は (YYYY-MM-DD または "", 注記)。
    """
    import datetime
    import re

    texts = [str(r.get("text", "")) for r in ocr_rows if r.get("start_sec", 0) < 180]
    day = month = year = None
    for t in texts:
        if day is None and (m := re.search(r"放送日\D{0,6}?(\d{1,2})\s*日", t)):
            day = int(m.group(1))
        if month is None and (m := re.search(r"(?:(\d{4})\s*)?年\s*(\d{1,2})\s*月", t)):
            month = int(m.group(2))
            year = int(m.group(1)) if m.group(1) else None
        if day and month:
            break
    if not (day and month):
        return "", "スレートから放送日を読めなかった"
    try:
        if year:
            d = datetime.date(year, month, day)
            note = "" if d.weekday() == 6 else f"スレートの日付が日曜ではない（{'月火水木金土日'[d.weekday()]}曜）"
            return d.isoformat(), note
        this_year = datetime.date.today().year
        sundays = [y for y in range(FIRST_YEAR, this_year + 1) if datetime.date(y, month, day).weekday() == 6]
    except ValueError:
        return "", f"スレートの日付が不正（{month}月{day}日）"
    if len(sundays) == 1:
        return f"{sundays[0]}-{month:02d}-{day:02d}", "年が読めないため、日曜になる年から決めた"
    return "", f"年が読めず、日曜になる年が {sundays} で1つに決まらない"


def _normalize_talent(name: str) -> str:
    """「村上健志（フルーツポンチ）」「ザ・マミイ 酒井貴士」のような揺れを、個人名だけにそろえる。"""
    import re
    name = re.sub(r"[（(].*?[）)]", "", str(name or ""))
    parts = name.split()
    if len(parts) >= 2 and any(k in parts[0] for k in ("・", "ザ", "NON", "ノンスタイル")):
        parts = parts[1:]  # 先頭のコンビ名を落とす
    return "".join(parts).strip()


NAME_TELOP_RE = __import__("re").compile(r"(さん|くん|ちゃん|君)\s*[\(（][^)）]*歳")
NAME_FRAME_CROP = "crop=1920:540:0:540"  # 名前テロップは画面の下半分に出る
NAME_FRAME_MAX = 40


def _name_telop_times(ocr_rows: list[dict]) -> list[float]:
    """OCR で「〇〇さん(〇歳)」が読めた時刻を、10秒以内の重複を除いて返す。"""
    times: list[float] = []
    for r in ocr_rows:
        if NAME_TELOP_RE.search(str(r.get("text", ""))):
            t = float(r["start_sec"]) + 1.0  # 出始めのアニメーションを避ける
            if not times or t - times[-1] > 10:
                times.append(t)
    return times[:NAME_FRAME_MAX]


def _grab_frame(video: Path, sec: float) -> bytes:
    import subprocess
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-ss", f"{sec:.2f}", "-i", str(video), "-frames:v", "1",
           "-vf", f"scale=1920:1080,{NAME_FRAME_CROP}", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def _talent_obj(t) -> dict:
    """Gemini の talents（文字列でも {"name","group"} でも）を {"name","group"} にそろえる。"""
    import re
    if isinstance(t, dict):
        name, group = str(t.get("name") or ""), str(t.get("group") or "")
    else:
        name, group = str(t or ""), ""
        if m := re.search(r"[（(](.*?)[）)]", name):
            group = m.group(1)
    return {"name": _normalize_talent(name), "group": group.strip()}


def _fmt(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def _lines(rows: list[dict]) -> str:
    out = []
    prev = None
    for r in rows:
        text = str(r.get("text", "")).strip()
        if not text:
            continue
        key = (int(r["start_sec"]), text)
        if key == prev:
            continue
        prev = key
        out.append(f"[{_fmt(r['start_sec'])}-{_fmt(r['end_sec'])}] {text}")
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) < 6:
        print(__doc__)
        sys.exit(1)
    episode_no = sys.argv[1]
    probe_dir = Path(sys.argv[2]).resolve()
    thumbs_dir = Path(sys.argv[3]).resolve()
    out_dir = Path(sys.argv[4]).resolve()
    video = Path(sys.argv[5]).resolve()
    if not video.exists():
        sys.exit(f"動画が見つかりません: {video}")
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr_rows = json.loads((probe_dir / "ocr_rows.json").read_text(encoding="utf-8"))
    audio_rows = json.loads((probe_dir / "audio_rows.json").read_text(encoding="utf-8"))
    thumbs = json.loads((thumbs_dir / "thumbnails.json").read_text(encoding="utf-8"))["thumbnails"]

    thumb_lines = []
    for t in thumbs:
        shown = [f"{_fmt(t['start_sec'])}-{_fmt(t['end_sec'])}"]
        shown += [f"{_fmt(a['start_sec'])}-{_fmt(a['end_sec'])}" for a in t["also_shown"]]
        thumb_lines.append(f"index={t['index']} 表示: {', '.join(shown)}")

    prompt = (
        PROMPT.format(genre_rules=GENRE_RULES)
        + f"\n\n## 入力\n回: #{episode_no}\n"
        + "\n### サムネ発表区間\n" + "\n".join(thumb_lines)
        + "\n\n### OCR（[開始-終了] テキスト）\n" + _lines(ocr_rows)
        + "\n\n### 文字起こし（[開始-終了] テキスト）\n" + _lines(audio_rows)
    )
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))
    from app import services
    from app.config import settings

    types = services.genai2_types
    contents: list = [prompt]
    for t in thumbs:  # サムネの文言を正確に読ませるため画像も渡す
        contents.append(f"サムネ画像 index={t['index']}")
        contents.append(types.Part.from_bytes(data=(thumbs_dir / t["file"]).read_bytes(), mime_type="image/jpeg"))

    name_times = _name_telop_times(ocr_rows)
    frames_dir = out_dir / "name_frames"
    frames_dir.mkdir(exist_ok=True)
    for sec in name_times:  # 名前は OCR が崩れやすいので画像でも読ませる
        jpg = _grab_frame(video, sec)
        (frames_dir / f"name_{int(sec):05d}.jpg").write_bytes(jpg)
        contents.append(f"名前テロップ画像 時刻={_fmt(sec)}")
        contents.append(types.Part.from_bytes(data=jpg, mime_type="image/jpeg"))
    print(f"名前テロップ画像: {len(name_times)} 枚")

    config = types.GenerateContentConfig(
        temperature=0,  # 実行のたびに名前や分類がぶれるのを抑える
        thinking_config=types.ThinkingConfig(thinking_budget=24576),
        max_output_tokens=65536,
    )
    settings.gemini_model = MODEL  # _generate_json_with_retries は settings.gemini_model を使う
    client = services.genai2.Client(api_key=settings.gemini_api_key)
    parsed, usage = services._generate_json_with_retries(
        client, contents, config, context=f" samunet#{episode_no}",
    )
    if not isinstance(parsed, dict):
        sys.exit("Gemini の結果を JSON として読めませんでした")
    parsed["episode_no"] = int(episode_no)
    ep = parsed.setdefault("episode", {})
    slate_date, slate_note = _slate_date(ocr_rows)
    ep["broadcast_date_gemini"] = ep.get("broadcast_date", "")
    if slate_date:
        ep["broadcast_date"] = slate_date
        ep["date_note"] = slate_note
    else:
        ep["date_note"] = f"{slate_note}。Gemini の値をそのまま使用"
    ep["talents"] = [_talent_obj(t) for t in ep.get("talents") or []]
    group_of = {t["name"]: t["group"] for t in ep["talents"]}
    ep["location"] = "".join(x for x in (ep.get("pref", ""), ep.get("city", "")) if x) + (
        f"（{ep['area']}）" if ep.get("area") else "")
    for r in parsed.get("rows", []):
        r["talents"] = [_talent_obj(t)["name"] for t in r.get("talents") or []]
        r["genre_raw"] = r.get("genre", "")
        r["genre"] = _normalize_genre(r.get("genre"))
    (out_dir / "rows.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")

    ep = parsed.get("episode", {})
    thumb_by_index = {t["index"]: t for t in thumbs}
    header = ["回", "放送日", "都道府県", "市区町村", "エリア", "取材場所", "取材タレント", "タレントのコンビ名", "名前", "年齢", "肩書・小見出し",
              "概要", "特徴", "特徴の根拠", "開始", "終了", "サムネ", "サムネ表示", "サムネの文言", "No.1", "名前の注記"]
    with (out_dir / "rows.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in parsed.get("rows", []):
            t = thumb_by_index.get(r.get("thumbnail_index"))
            w.writerow([
                episode_no, ep.get("broadcast_date", ""), ep.get("pref", ""), ep.get("city", ""), ep.get("area", ""),
                r.get("place", ""),
                "・".join(r.get("talents") or []),
                "・".join(group_of.get(n, "") for n in (r.get("talents") or [])),
                "・".join(r.get("names") or []),
                "・".join("" if a is None else str(a) for a in (r.get("ages") or [])),
                r.get("title", ""), r.get("summary", ""), r.get("genre", ""), r.get("genre_reason", ""),
                _fmt(r.get("start_sec") or 0), _fmt(r.get("end_sec") or 0),
                t["file"] if t else "", _fmt(t["start_sec"]) if t else "", r.get("thumbnail_copy", ""),
                "○" if r.get("is_top") else "", r.get("name_note", ""),
            ])

    if usage:
        cost = (usage["input_tokens"] * PRICE_IN_USD_PER_1M
                + (usage["total_tokens"] - usage["input_tokens"]) * PRICE_OUT_USD_PER_1M) / 1e6
        print(f"モデル: {MODEL}  トークン: {usage}  概算 ${cost:.3f}")
    print(f"{len(parsed.get('rows', []))} 行を保存: {out_dir}")


if __name__ == "__main__":
    main()
