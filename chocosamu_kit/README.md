# チョコサム データベース

「超町人！チョコレートサムネット」の放送回を分析し、超町人（取材された一般の方）ごとに、サムネ・名前・特徴（ジャンル）・ロケ内容・ロケ地・取材タレント・放送回を一覧・検索できる社内サイトを作るプロジェクト。

このフォルダは、video_analyzer リポジトリの `samunet/` で行った調査（2026-11-18〜19）を、新しいリポジトリに移すためにまとめたもの。

## 中身

| パス | 内容 |
|---|---|
| `analysis/find_thumbnails.py` | 動画からサムネ発表画面を見つけて、サムネを切り出す（GPU・AI 不要） |
| `analysis/reveal_ref.rgb` | 発表画面かどうかの判定に使う基準データ（`find_thumbnails.py` が読む） |
| `analysis/samunet_probe.py` | 動画に OCR（テロップ）と Whisper（文字起こし）をかける（GPU 推奨） |
| `analysis/samunet_summarize.py` | OCR・文字起こし・サムネを Gemini に渡し、超町人ごとに1行の表を作る（有料） |
| `analysis/build_site_data.py` | 回ごとの結果を1つにまとめ、サイト用の `data.json` と全回の CSV を作る |
| `analysis/upload_results.py` | 結果を非公開バケット `s3://chocosamu-data/` に上げる（AWS CLI、プロファイル `chocosamu-uploader`） |
| `docs/findings.md` | 調査で分かったこと（精度、注意点、残っている課題） |
| `docs/genre_rules.md` | 「特徴」（ジャンル）の分類ルールと、担当者が確認した判断例 |
| `docs/data_model.md` | 出力データの形（`rows.json`、`data.json`、CSV の列） |
| `docs/site_plan.md` | データベースサイトの構想（AWS 構成、段階、確認事項） |
| `docs/site_requirements.md` | **サイトを作る人向けの要件と引き継ぎ事項**（表示項目、検索、守ること、データ品質、未決定事項） |
| `CLAUDE.md` | このリポジトリで作業する Claude 向けの要点 |
| `sample_data/` | 5回分の分析結果のサンプル（実データ）。一般の方の名前・顔を含む。**リポジトリが非公開であることを前提に**コミットしている |

## 分析の流れ（1回分）

```
動画(.mp4)
  ├─ find_thumbnails.py ─────▶ thumbs/<回>/thumb_XX.jpg, thumbnails.json    数分・CPU のみ
  ├─ samunet_probe.py ───────▶ probe_out/<回>/ocr_rows.json, audio_rows.json 15〜30分・GPU
  └─ samunet_summarize.py ───▶ rows/<回>/rows.json, rows.csv, name_frames/  1〜2分・Gemini 約0.2ドル
                                         │
            build_site_data.py（全回まとめて）▶ site_data/data.json, all_rows.csv, thumbs/
```

### 実行例（分析用PC、PowerShell）

```powershell
$py  = "E:\Projects\video_analyzer\.venv\Scripts\python"
$dir = "D:\Projects\チョコサム分析"

python analysis\find_thumbnails.py "$dir\#04.mp4" "$dir\thumbs\#04"
& $py analysis\samunet_probe.py     "$dir\#04.mp4" "$dir\probe_out\#04"
& $py analysis\samunet_summarize.py 4 "$dir\probe_out\#04" "$dir\thumbs\#04" "$dir\rows\#4" "$dir\#04.mp4"

# 全回をまとめる（rows と thumbs のフォルダ名が違う回は「rowsの名前=thumbsの名前」で対応させる）
python analysis\build_site_data.py "$dir\rows" "$dir\thumbs" "$dir\site_data" "#2=#02" "#4=#04"

# S3 に上げる（回ごとの結果と、サイト用データ）
python analysis\upload_results.py episode 4 "$dir\rows\#4" "$dir\thumbs\#04"
python analysis\upload_results.py site "$dir\site_data"
```

## 置き場所

| もの | 場所 |
|---|---|
| 動画 | 分析用PC `D:\Projects\チョコサム分析\過去動画\過去回\#NN\`（S3 には上げない。元は Dropbox） |
| 分析結果・サイト用データ | `s3://chocosamu-data/`（非公開）。5回分（#02 #04 #40 #80 #126）のサンプルを上げ済み |

各回のフォルダには、動画のほかに CUE シート（xlsx）、`〇〇_CM.txt`（CM の位置）、配信用テキスト（番組概要の docx）、配信用サムネ画像も入っている。名前・内容の確認や、視聴率との時刻合わせに使える。

動画が手元にない回（2026-11-19 時点）：#2・#3（フォルダなし）、#62・#66・#73・#101・#111（動画なし）。#02 は約14分で途中切れ。

## 依存関係（移設するときの注意）

- `samunet_probe.py` と `samunet_summarize.py` は、**video_analyzer の `app/services.py` をそのまま呼んでいる**（Whisper・PaddleOCR・Gemini の呼び出しと、`.env` / `.env.local` の設定）。
  - video_analyzer の場所は環境変数 `VIDEO_ANALYZER_DIR` で指定する（既定は `E:\Projects\video_analyzer`）。実行には video_analyzer の仮想環境（`.venv`）を使う。
  - 新しいリポジトリで独立させるなら、`services.py` の次の関数を移植する：`transcribe_video`・`_segment_rows`（Whisper）、`_extract_ocr_rows` とそのフィルタ・まとめ処理（OCR）、`_generate_json_with_retries`（Gemini）。`services.py` は一時フォルダが `A:\tmp` 固定なので、設定で変えられるようにする。
- `find_thumbnails.py` と `build_site_data.py` は ffmpeg と Python 標準ライブラリだけで動く。
- `samunet_summarize.py` は `gemini-2.5-pro` を固定で使う（毎日の分析の `GEMINI_MODEL` とは別）。API キーは video_analyzer の `.env` から読む。

## 分析用PCで動かすときの決まり（video_analyzer と同居している間）

- **毎日の分析（`worker.py` / `backfill.py`）と GPU を取り合わないよう、平日 6:00〜18:00 に動かす。** 平日 18:00〜翌6:00 と、金曜 18:00〜月曜 6:00 は毎日の分析が動いている。
- 始める前に、毎日の分析が止まっていることを確認する（何も出なければ OK）：
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'backfill\.py|worker\.py' } | Select-Object ProcessId, CommandLine
  ```
- **スクリプトの名前に `worker` / `backfill` を入れない。** 平日 6:00 に `stop_worker.ps1` がプロセスを名前で探して止めるため。
- これらのスクリプトは S3 に何も書かない。video_analyzer の `worker.py` / `backfill.py` に動画を流すと、結果がニュース分析のダッシュボードに混ざるので使わない。
