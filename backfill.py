"""既存のS3動画をまとめて処理するバックフィルスクリプト

worker.py（SQS監視）と同じ処理ロジックを再利用し、S3の movie/ 配下にある
既存動画を日付順に1本ずつ処理する。GPUを共有するため、実行前に worker.py は
停止しておくこと。

起動方法:
    .venv/Scripts/python backfill.py
    .venv/Scripts/python backfill.py --since 20260710 --channels ch1,ch4,ch6
    .venv/Scripts/python backfill.py --dry-run   # 対象一覧の確認のみ（処理はしない）
    .venv/Scripts/python backfill.py --time-windows "05:30-08:30,15:30-19:00"
        # 番組の放送時間帯が指定レンジに1秒でもかぶる動画だけを対象にする
    .venv/Scripts/python backfill.py --start-worker-after
        # バックフィル完了後、そのまま worker.py（SQS常駐）を起動する

視聴率ダッシュボードからの分析リクエスト:
    s3://{S3_BUCKET}/requests/analyze_requests.csv に、分析してほしい動画のS3キーを
    1行に1つ書き込んでおくと、次回バックフィル実行時に時間帯フィルタ・--since期間を
    無視して最優先で処理される（REQUEST_CSV_KEY参照）。
    ※ video-analyzer-worker用IAMロールに requests/* への s3:GetObject 権限が必要。
"""

import argparse
import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.config import settings
from worker import (
    VIDEO_EXTENSIONS,
    _build_boto_session,
    _effective_windows,
    _filename_start_sec,
    _load_hung_videos,
    _may_overlap_time_windows,
    _overlaps_time_windows,
    _parse_time_windows,
    _probe_duration_sec_url,
    _process_s3_object,
    _video_priority_key,
    _video_source_url,
    drain_sqs_batch,
    log,
    run_worker,
)

# バックフィル中、これだけ動画を処理するたびにSQSも覗いて処理する。
# backfillが数日がかりになっても、SQSのメッセージ保持期間（既定4日）内に
# 新規アップロード分の通知を消費できるようにするための対策。
_SQS_DRAIN_INTERVAL = 10

DEFAULT_SINCE = "20260710"
DEFAULT_CHANNELS = "ch1,ch4,ch6"
FAILED_LOG_PATH = Path("backfill_failed.txt")

# 視聴率ダッシュボードが「分析してほしい動画」のS3キーを書き込むCSV。
# 1行に1つ、movie/配下のS3キーをそのまま書く（ヘッダー行や動画以外の行は無視する）。
# 通常の時間帯フィルタ・--since期間を無視して必ず処理対象にする。
REQUEST_CSV_KEY = "requests/analyze_requests.csv"


def _load_requested_videos(s3, bucket: str) -> list[str]:
    """視聴率ダッシュボードが書き込んだ「分析してほしい動画」のS3キー一覧を読み込む。

    ファイルが無ければ空リストを返す（リクエストが無いだけで、エラーではない）。
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=REQUEST_CSV_KEY)["Body"].read().decode("utf-8-sig")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404"}:
            return []
        if code == "AccessDenied":
            log.warning(
                "リクエストCSV(%s)へのアクセス権限がありません。IAMポリシーに"
                "s3:GetObject(対象: %s)を追加してください。今回はリクエスト無しとして続行します。",
                REQUEST_CSV_KEY, REQUEST_CSV_KEY,
            )
            return []
        raise

    keys: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if Path(line).suffix.lower() not in VIDEO_EXTENSIONS:
            continue  # ヘッダー行など、動画キーでない行は無視する
        keys.append(line)
    return keys


def _filter_by_time_windows(
    s3, bucket: str, videos: list[str], windows: list[tuple[int, int]]
) -> list[str]:
    """番組の放送時間帯（ファイル名の開始時刻＋実尺）が指定ウィンドウに重なる動画だけ残す。"""
    candidates = []
    for key in videos:
        start_sec = _filename_start_sec(key)
        if start_sec is None:
            continue
        # 実尺を知らなくても明らかに重なりようがないものは事前に除外する
        # （曜日・日付限定の特例ウィンドウも、該当する動画だけここで合成される）
        if not _may_overlap_time_windows(start_sec, _effective_windows(key, windows)):
            continue
        candidates.append((key, start_sec))

    log.info("時間帯フィルタ: 事前絞り込み %d本 → 実尺確認対象", len(candidates))

    kept = []
    for i, (key, start_sec) in enumerate(candidates, 1):
        try:
            url = _video_source_url(s3, key)
            duration = _probe_duration_sec_url(url)
        except Exception:
            log.exception("尺取得に失敗、対象から除外します: %s", key)
            continue
        if duration <= 0:
            log.warning("尺取得できず(0秒)、対象から除外します: %s", key)
            continue
        if _overlaps_time_windows(start_sec, duration, _effective_windows(key, windows)):
            kept.append(key)
        if i % 20 == 0 or i == len(candidates):
            log.info("時間帯フィルタ: 実尺確認 %d/%d本 完了（一致 %d本）", i, len(candidates), len(kept))

    return kept


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


def run_backfill(
    since: str,
    channels: set[str] | None,
    dry_run: bool,
    time_windows: list[tuple[int, int]] | None = None,
) -> None:
    session = _build_boto_session()
    s3 = session.client("s3")

    targets = _list_target_videos(s3, settings.s3_bucket, since, channels)
    done = _list_already_processed(settings.s3_bucket)

    # 視聴率ダッシュボードから明示的にリクエストされた動画は、通常のフィルタ(時間帯・--since期間)
    # を無視して最優先で処理する。通常のpendingと重複しないよう、ここで抜いておく。
    requested = [key for key in _load_requested_videos(s3, settings.s3_bucket) if Path(key).stem not in done]
    requested_set = set(requested)
    if requested:
        log.info("視聴率ダッシュボードからのリクエスト: %d本（最優先で処理）", len(requested))

    not_done = [key for key in targets if Path(key).stem not in done]
    pending = [key for key in not_done if key not in requested_set]
    skipped = len(targets) - len(not_done)

    log.info(
        "バックフィル対象: %d本（該当%d本中、処理済み%d本をスキップ、リクエスト分%d本は別途処理）",
        len(pending), len(targets), skipped, len(requested),
    )

    if time_windows or settings.process_time_windows_extra:
        before = len(pending)
        pending = _filter_by_time_windows(s3, settings.s3_bucket, pending, time_windows or [])
        log.info("時間帯フィルタ適用: %d本 → %d本", before, len(pending))
        # 日付→時間帯(午前/午後)→チャンネル(ch1,ch4,ch6)の優先順で並べ替える
        pending.sort(key=lambda key: _video_priority_key(key, time_windows or []))

    # 過去にハングしたと記録された動画は後回しにする。通常の未処理分を先に片付けた後、
    # run_worker()（リアルタイムSQS）に切り替わる前に、ハング済みの動画も1回だけ再挑戦する。
    hung_videos = _load_hung_videos()
    non_hung_pending = [key for key in pending if key not in hung_videos]
    hung_pending = [key for key in pending if key in hung_videos]
    if hung_pending:
        log.info(
            "ハング済みのため後回し: %d本（通常対象%d本を処理した後に再挑戦します）",
            len(hung_pending), len(non_hung_pending),
        )

    if dry_run:
        for key in requested:
            log.info("  対象(リクエスト・最優先): %s", key)
        for key in non_hung_pending:
            log.info("  対象: %s", key)
        for key in hung_pending:
            log.info("  対象(ハング済み・後回し): %s", key)
        return

    sqs = None
    if settings.sqs_queue_url:
        sqs = session.client("sqs")

    failed: list[str] = []

    def _run_batch(batch: list[str], allow_hung: bool, bypass_time_filter: bool = False) -> None:
        for i, key in enumerate(batch, 1):
            log.info("[%d/%d] 処理開始: %s", i, len(batch), key)
            try:
                _process_s3_object(key, allow_hung=allow_hung, bypass_time_filter=bypass_time_filter)
            except Exception:
                log.exception("処理失敗、スキップして続行します: %s", key)
                failed.append(key)

            if sqs is not None and i % _SQS_DRAIN_INTERVAL == 0:
                try:
                    drained = drain_sqs_batch(sqs, time_windows, wait_time_sec=1)
                    if drained:
                        log.info("バックフィルの合間にSQSから%d件処理しました", drained)
                except Exception:
                    log.exception("バックフィル中のSQSドレインに失敗しました（続行します）")

    if requested:
        log.info("視聴率ダッシュボードからのリクエスト(%d本)を最優先で処理します", len(requested))
        _run_batch(requested, allow_hung=True, bypass_time_filter=True)

    _run_batch(non_hung_pending, allow_hung=False)

    if hung_pending:
        log.info("通常対象の処理が完了。ハング済み動画(%d本)を再挑戦します", len(hung_pending))
        _run_batch(hung_pending, allow_hung=True)

    total = len(requested) + len(non_hung_pending) + len(hung_pending)
    log.info(
        "バックフィル完了: 成功%d本 / 失敗%d本",
        total - len(failed), len(failed),
    )
    if failed:
        FAILED_LOG_PATH.write_text("\n".join(failed), encoding="utf-8")
        log.warning("失敗した動画一覧を %s に保存しました", FAILED_LOG_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="既存S3動画のバックフィル処理")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="この日付(YYYYMMDD)以降を対象にする")
    parser.add_argument("--channels", default=DEFAULT_CHANNELS, help="対象チャンネル（カンマ区切り、空で全チャンネル）")
    parser.add_argument("--dry-run", action="store_true", help="対象一覧の確認のみ行い、実際の処理はしない")
    parser.add_argument(
        "--time-windows",
        default=None,
        help='番組の放送時間帯がこのレンジにかぶる動画だけ対象にする。例: "05:30-08:30,15:30-19:00"'
        "（省略時は.envのPROCESS_TIME_WINDOWSを使う）",
    )
    parser.add_argument(
        "--start-worker-after",
        action="store_true",
        help="バックフィル完了後、そのままworker.py（SQS常駐）を起動し続ける",
    )
    args = parser.parse_args()

    channels = {c.strip() for c in args.channels.split(",") if c.strip()} or None
    time_windows_spec = args.time_windows or settings.process_time_windows
    time_windows = _parse_time_windows(time_windows_spec) if time_windows_spec else None
    run_backfill(args.since, channels, args.dry_run, time_windows)

    if args.start_worker_after and not args.dry_run:
        log.info("バックフィル完了。続けてworker.py（SQS常駐）を起動します。")
        run_worker()


if __name__ == "__main__":
    main()
