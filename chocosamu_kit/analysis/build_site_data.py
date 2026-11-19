"""回ごとの分析結果（rows.json とサムネ）を1つにまとめ、サイトが読む data.json と、全回の CSV を作る。

入力（回ごとのフォルダ。名前は何でもよい。rows.json の中の回番号を使う）:
    rows_root/<任意>/rows.json         samunet_summarize.py の出力
    thumbs_root/<任意>/thumbnails.json  find_thumbnails.py の出力（rows_root と同じフォルダ名）
出力（out_dir）:
    data.json          サイト用（episodes と people の2つの配列）
    all_rows.csv       全回の1超町人＝1行（Excel で開ける UTF-8 BOM 付き）
    thumbs/<回3桁>/thumb_XX.jpg   サムネのコピー（data.json からは相対パスで参照）

Python 標準ライブラリだけで動く（GPU・Gemini 不要）。

使い方:
    python analysis/build_site_data.py <rows_root> <thumbs_root> <out_dir> [フォルダ名=回番号 ...]
    例: python analysis/build_site_data.py D:\\chocosamu\\rows D:\\chocosamu\\thumbs D:\\chocosamu\\site_data
"""

import csv
import json
import shutil
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    rows_root, thumbs_root, out_dir = (Path(a).resolve() for a in sys.argv[1:4])
    # rows のフォルダ名と thumbs のフォルダ名が違う場合の対応（例: "#4=Chocolate-...-126"）
    thumb_dir_of = dict(a.split("=", 1) for a in sys.argv[4:])
    out_dir.mkdir(parents=True, exist_ok=True)

    episodes, people = [], []
    for rows_json in sorted(rows_root.glob("*/rows.json")):
        folder = rows_json.parent.name
        data = json.loads(rows_json.read_text(encoding="utf-8"))
        ep = data.get("episode", {})
        ep_no = int(data.get("episode_no") or ep.get("episode_no") or "".join(c for c in folder if c.isdigit()))
        thumbs_dir = thumbs_root / thumb_dir_of.get(folder, folder)
        thumbs = {}
        if (thumbs_dir / "thumbnails.json").exists():
            thumbs = {t["index"]: t for t in json.loads(
                (thumbs_dir / "thumbnails.json").read_text(encoding="utf-8"))["thumbnails"]}
        else:
            print(f"注意: サムネが見つかりません: {thumbs_dir}")

        episodes.append({
            "episode": ep_no,
            "broadcast_date": ep.get("broadcast_date", ""),
            "date_note": ep.get("date_note", ""),
            "pref": ep.get("pref", ""), "city": ep.get("city", ""), "area": ep.get("area", ""),
            "talents": ep.get("talents", []),
            "mc": ep.get("mc", []),
        })
        rows = sorted(data.get("rows", []), key=lambda r: r.get("start_sec") or 0)
        for order, r in enumerate(rows, 1):
            t = thumbs.get(r.get("thumbnail_index"))
            thumb_rel = ""
            if t:
                dest = out_dir / "thumbs" / f"{ep_no:03d}" / t["file"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(thumbs_dir / t["file"], dest)
                thumb_rel = dest.relative_to(out_dir).as_posix()
            people.append({
                "id": f"{ep_no:03d}-{order:02d}",
                "episode": ep_no,
                "order": order,
                "names": r.get("names", []),
                "ages": r.get("ages", []),
                "name_note": r.get("name_note", ""),
                "title": r.get("title", ""),
                "place": r.get("place", ""),
                "talents": r.get("talents", []),
                "summary": r.get("summary", ""),
                "genres": [g for g in str(r.get("genre", "")).split("、") if g and g != "該当なし"],
                "genre_reason": r.get("genre_reason", ""),
                "start_sec": r.get("start_sec"),
                "end_sec": r.get("end_sec"),
                "thumbnail": thumb_rel,
                "thumbnail_shown_sec": t["start_sec"] if t else None,
                "thumbnail_copy": r.get("thumbnail_copy", ""),
                "is_top": bool(r.get("is_top")),
            })

    episodes.sort(key=lambda e: e["episode"])
    people.sort(key=lambda p: (p["episode"], p["order"]))
    (out_dir / "data.json").write_text(
        json.dumps({"episodes": episodes, "people": people}, ensure_ascii=False, indent=1), encoding="utf-8")

    ep_by_no = {e["episode"]: e for e in episodes}
    with (out_dir / "all_rows.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ID", "回", "放送日", "都道府県", "市区町村", "エリア", "取材場所", "取材タレント", "タレントのコンビ名",
                    "名前", "年齢", "名前の注記", "肩書・小見出し", "概要", "特徴", "特徴の根拠",
                    "開始秒", "終了秒", "サムネ", "サムネの文言", "No.1"])
        for p in people:
            e = ep_by_no[p["episode"]]
            group_of = {t["name"]: t.get("group", "") for t in e["talents"] if isinstance(t, dict)}
            w.writerow([
                p["id"], p["episode"], e["broadcast_date"], e["pref"], e["city"], e["area"], p["place"],
                "・".join(p["talents"]), "・".join(group_of.get(n, "") for n in p["talents"]),
                "・".join(p["names"]), "・".join("" if a is None else str(a) for a in p["ages"]), p["name_note"],
                p["title"], p["summary"], "、".join(p["genres"]) or "該当なし", p["genre_reason"],
                p["start_sec"], p["end_sec"], p["thumbnail"], p["thumbnail_copy"], "○" if p["is_top"] else "",
            ])
    print(f"{len(episodes)} 回・{len(people)} 人を保存: {out_dir}")


if __name__ == "__main__":
    main()
