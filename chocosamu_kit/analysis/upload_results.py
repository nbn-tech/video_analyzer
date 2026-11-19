"""分析結果を S3（非公開バケット chocosamu-data）に上げる。AWS CLI の `aws s3 sync` を使う。

S3 の構成:
    s3://chocosamu-data/results/<回3桁>/rows.json, rows.csv, prompt.txt
    s3://chocosamu-data/results/<回3桁>/name_frames/name_XXXXX.jpg   名前の確認用画像
    s3://chocosamu-data/results/<回3桁>/thumbs/thumb_XX.jpg, thumbnails.json
    s3://chocosamu-data/site/data.json, all_rows.csv, thumbs/<回3桁>/thumb_XX.jpg   サイト用（build_site_data.py の出力）

使い方:
    # 1回分の結果を上げる
    python analysis/upload_results.py episode 4 D:\\chocosamu\\rows\\#4 D:\\chocosamu\\thumbs\\#04
    # サイト用データ（全回まとめ）を上げる
    python analysis/upload_results.py site D:\\chocosamu\\site_data
    # 実際には上げずに、何が上がるかだけ表示する
    python analysis/upload_results.py --dryrun site D:\\chocosamu\\site_data

設定（環境変数）:
    CHOCOSAMU_BUCKET   既定 chocosamu-data
    CHOCOSAMU_PROFILE  既定 chocosamu-uploader（chocosamu-data だけ読み書きできる IAM ユーザー）

- 同期先の古いファイルは消す（--delete）。バケットはバージョニングが有効なので、消した版も戻せる。
- このバケットは非公開（パブリックアクセスをすべてブロック）。一般の方の名前・顔を含むので、公開設定にしない。
"""

import os
import subprocess
import sys
from pathlib import Path

BUCKET = os.environ.get("CHOCOSAMU_BUCKET", "chocosamu-data")
PROFILE = os.environ.get("CHOCOSAMU_PROFILE", "chocosamu-uploader")


def _sync(src: Path, dest: str, dryrun: bool, excludes: list[str] = ()) -> None:
    if not src.is_dir():
        sys.exit(f"フォルダが見つかりません: {src}")
    cmd = ["aws", "--profile", PROFILE, "s3", "sync", str(src), f"s3://{BUCKET}/{dest}", "--delete", "--only-show-errors"]
    for pattern in excludes:
        cmd += ["--exclude", pattern]
    if dryrun:
        cmd.remove("--only-show-errors")
        cmd.append("--dryrun")
    print(f"{src} → s3://{BUCKET}/{dest}")
    subprocess.run(cmd, check=True)


def main() -> None:
    args = sys.argv[1:]
    dryrun = "--dryrun" in args
    args = [a for a in args if a != "--dryrun"]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "episode" and len(args) == 4:
        ep = int(args[1])
        rows_dir, thumbs_dir = Path(args[2]).resolve(), Path(args[3]).resolve()
        _sync(rows_dir, f"results/{ep:03d}/", dryrun, excludes=["thumbs/*"])
        _sync(thumbs_dir, f"results/{ep:03d}/thumbs/", dryrun)
    elif args[0] == "site" and len(args) == 2:
        _sync(Path(args[1]).resolve(), "site/", dryrun)
    else:
        print(__doc__)
        sys.exit(1)
    print("完了" if not dryrun else "（--dryrun のため何も上げていません）")


if __name__ == "__main__":
    main()
