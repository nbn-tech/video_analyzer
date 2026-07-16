"""既存のS3動画をまとめて処理するバックフィルスクリプト

worker.py（SQS監視）と同じ処理ロジックを再利用し、S3の movie/ 配下にある
既存動画を日付順に1本ずつ処理する。GPUを共有するため、実行前に worker.py は
停止しておくこと。

起動方法:
    .venv/Scripts/python backfill.py
    .venv/Scripts/python backfill.py --since 20260710 --channels ch1,ch4,ch6
    .venv/Scripts/python backfill.py --dry-run   # 対象一覧の確認のみ（処理はしない）
"""

import argparse
import logging
from pathlib import Path

import boto3

from app.config import settings
from worker import VIDEO_EXTENSIONS, _build_boto_session, _process_s3_object, log

DEFAULT_SINCE = "20260710"
DEFAULT_CHANNELS = "ch1,ch4,ch6"
FAILED_LOG_PATH = Path("backfill_failed.txt")


def _list_target_videos(s3, bucket: str, since: str, channels: set[str] | None) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    videos: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix="movie/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) < 4:
                continue
            channel, date = parts[1], parts[2]
            if channels and channel not in channels:
                continue
            if date < since:
                continue
            if Path(key).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            videos.append(key)

    videos.sort(key=lambda k: (k.split("/")[2], k))
    return videos


def _list_already_processed(bucket: str) -> set[str]:
    """results/ 配下の既存CSVから処理済みファイル名(stem)を集める。

    worker.py用IAMロールは results/ を list できないため、
    Athena検索用ロール（read権限あり）で一覧する。
    """
    session = boto3.Session(
        profile_name=settings.athena_aws_profile or None,
        region_name=settings.aws_region,
    )
    s3 = session.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    done: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix="results/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("_corners.csv"):
                done.add(Path(key).stem.removesuffix("_corners"))
    return done


def run_backfill(since: str, channels: set[str] | None, dry_run: bool) -> None:
    session = _build_boto_session()
    s3 = session.client("s3")

    targets = _list_target_videos(s3, settings.s3_bucket, since, channels)
    done = _list_already_processed(settings.s3_bucket)
    pending = [key for key in targets if Path(key).stem not in done]
    skipped = len(targets) - len(pending)

    log.info(
        "バックフィル対象: %d本（該当%d本中、処理済み%d本をスキップ）",
        len(pending), len(targets), skipped,
    )

    if dry_run:
        for key in pending:
            log.info("  対象: %s", key)
        return

    failed: list[str] = []
    for i, key in enumerate(pending, 1):
        log.info("[%d/%d] 処理開始: %s", i, len(pending), key)
        try:
            _process_s3_object(key)
        except Exception:
            log.exception("処理失敗、スキップして続行します: %s", key)
            failed.append(key)

    log.info(
        "バックフィル完了: 成功%d本 / 失敗%d本",
        len(pending) - len(failed), len(failed),
    )
    if failed:
        FAILED_LOG_PATH.write_text("\n".join(failed), encoding="utf-8")
        log.warning("失敗した動画一覧を %s に保存しました", FAILED_LOG_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="既存S3動画のバックフィル処理")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="この日付(YYYYMMDD)以降を対象にする")
    parser.add_argument("--channels", default=DEFAULT_CHANNELS, help="対象チャンネル（カンマ区切り、空で全チャンネル）")
    parser.add_argument("--dry-run", action="store_true", help="対象一覧の確認のみ行い、実際の処理はしない")
    args = parser.parse_args()

    channels = {c.strip() for c in args.channels.split(",") if c.strip()} or None
    run_backfill(args.since, channels, args.dry_run)


if __name__ == "__main__":
    main()
