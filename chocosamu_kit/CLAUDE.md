# チョコサムまとめサイト

「超町人！チョコレートサムネット」の放送回を、超町人（取材された一般の方）ごとに一覧・検索できる社内サイトと、その元データを作る分析スクリプトのリポジトリ。

## 最初に読むもの

1. `docs/site_requirements.md` — サイトの要件、守ること、決まっていないこと（**サイトを作るならまずこれ**）
2. `docs/data_model.md` — `data.json` の形
3. `docs/site_plan.md` — AWS 構成と決定の経緯
4. `docs/genre_rules.md` — 特徴（ジャンル）の定義
5. `docs/findings.md` — 分析の精度と既知の問題
6. `README.md` — 分析スクリプトの使い方

## 必ず守ること

- **一般の方の名前・顔を扱う。サイトはログイン必須。** S3 バケット `chocosamu-data` を公開設定にしない。バケットポリシーに `Principal: "*"` を入れない。サイトは CloudFront の OAC 経由で `site/*` だけ読む。
- `sample_data/`（5回分の実データ。一般の方の名前・顔を含む）はコミットしてある。**このリポジトリは非公開のまま運用する**。公開リポジトリやフォークに出さない。
- 作業用の出力（`site_data/`・`rows/`・`thumbs/`・`probe_out/`）と動画はコミットしない（`.gitignore` 済み）。全回のデータは S3 に置く。
- 分析スクリプト（`analysis/`）は分析用PCでだけ動かす。GPU を使う処理（`samunet_probe.py`）は平日 6:00〜18:00 に限る（毎日のニュース分析と同じPCのため）。スクリプト名に `worker` / `backfill` を入れない。
- `samunet_summarize.py` は Gemini を呼ぶ（有料、1回約0.2ドル）。何本も流す前に確認する。
- `bangumi-info` バケット（video_analyzer 用）は一般公開されているので、チョコサムのデータを置かない。

## データ

- 本番：`s3://chocosamu-data/site/data.json`（＋ `site/thumbs/`）。AWS CLI プロファイル `chocosamu-uploader`（分析用PCのみ）。
- 開発用：`sample_data/site_data/data.json`（5回・36組）。

## ジャンルのルールを変えるとき

`analysis/samunet_summarize.py` の `GENRE_RULES` と `docs/genre_rules.md` の両方を直す。判断例は番組担当者の判断を正解として足す。
