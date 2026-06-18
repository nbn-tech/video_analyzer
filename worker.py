"""
S3→SQS→処理→S3 CSV出力 ワーカー

起動方法:
    .venv\Scripts\python worker.py

S3にmp4がアップロードされるとSQS経由で通知を受け取り、
動画を処理してコーナー分類結果をCSVとしてS3に出力する。
"""

import csv
import io
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote_plus

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import boto3

from app.config import settings

if settings.paddle_pdx_home:
    os.environ.setdefault("PADDLE_PDX_HOME", settings.paddle_pdx_home)
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", settings.paddle_pdx_home)

from app.services import segment_corners

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".ts", ".m2ts", ".mts"}


def _build_boto_kwargs() -> dict:
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


def _corners_to_csv(filename: str, corners: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["filename", "start_sec", "end_sec", "title", "summary", "tags"])
    for c in corners:
        tags = c.get("tags", [])
        tags_str = "|".join(tags) if isinstance(tags, list) else str(tags)
        writer.writerow([
            filename,
            f"{c['start_sec']:.3f}",
            f"{c['end_sec']:.3f}",
            c.get("title", ""),
            c.get("summary", ""),
            tags_str,
        ])
    return buf.getvalue()


def _build_output_key(s3_key: str) -> str:
    """
    入力キーから出力CSVキーを生成する。

    movie/ch1/20260617/video.mp4
      → results/nbn/20260617/video_corners.csv
    """
    parts = s3_key.split("/")
    # 動画直上のディレクトリを日付として使う
    date_dir = parts[-2] if len(parts) >= 2 else "unknown"
    stem = Path(s3_key).stem
    base = settings.s3_output_prefix.rstrip("/")
    station = settings.s3_output_station.strip("/")
    return f"{base}/{station}/{date_dir}/{stem}_corners.csv"


def _process_s3_object(s3_key: str) -> None:
    boto_kwargs = _build_boto_kwargs()
    s3 = boto3.client("s3", **boto_kwargs)

    ext = Path(s3_key).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        log.info("スキップ（動画以外）: %s", s3_key)
        return

    log.info("処理開始: s3://%s/%s", settings.s3_bucket, s3_key)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_video = Path(tmpdir) / Path(s3_key).name
        log.info("ダウンロード中...")
        s3.download_file(settings.s3_bucket, s3_key, str(local_video))
        log.info("ダウンロード完了: %s", local_video)

        analyzed = segment_corners(local_video)

        corners = analyzed.get("corners", [])
        log.info("コーナー数: %d", len(corners))

        csv_content = _corners_to_csv(Path(s3_key).name, corners)

        output_key = _build_output_key(s3_key)
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=output_key,
            Body=csv_content.encode("utf-8-sig"),  # BOM付きUTF-8（Excel対応）
            ContentType="text/csv; charset=utf-8",
        )
        log.info("CSV出力完了: s3://%s/%s", settings.s3_bucket, output_key)


def _parse_s3_records(body: str) -> list[str]:
    """SQSメッセージ本文からS3キー一覧を取得する。"""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("JSONパース失敗: %s", body[:200])
        return []

    # SNS経由の場合はネストされている
    if "Message" in data:
        try:
            data = json.loads(data["Message"])
        except (json.JSONDecodeError, TypeError):
            pass

    keys = []
    for record in data.get("Records", []):
        s3_info = record.get("s3", {})
        key = s3_info.get("object", {}).get("key", "")
        if key:
            keys.append(unquote_plus(key))

    return keys


def run_worker() -> None:
    if not settings.sqs_queue_url:
        log.error("SQS_QUEUE_URL が設定されていません。.env.local を確認してください。")
        sys.exit(1)
    if not settings.s3_bucket:
        log.error("S3_BUCKET が設定されていません。.env.local を確認してください。")
        sys.exit(1)

    boto_kwargs = _build_boto_kwargs()
    sqs = boto3.client("sqs", **boto_kwargs)

    log.info("ワーカー起動。SQSポーリング中: %s", settings.sqs_queue_url)

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=settings.sqs_queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,  # ロングポーリング（コスト削減）
                VisibilityTimeout=3600,  # 1時間（長い動画対応）
            )
        except Exception as exc:
            log.error("SQS受信エラー: %s", exc)
            time.sleep(30)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        msg = messages[0]
        receipt = msg["ReceiptHandle"]

        try:
            s3_keys = _parse_s3_records(msg["Body"])
            if not s3_keys:
                log.info("S3レコードなし（テストメッセージ等）、スキップ")
            else:
                for key in s3_keys:
                    _process_s3_object(key)

            sqs.delete_message(QueueUrl=settings.sqs_queue_url, ReceiptHandle=receipt)

        except Exception as exc:
            log.exception("処理エラー（メッセージはキューに残します）: %s", exc)


if __name__ == "__main__":
    run_worker()
