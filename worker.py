"""
S3->SQS->処理->S3 CSV出力 ワーカー

起動方法:
    .venv/Scripts/python worker.py

S3にmp4がアップロードされるとSQS経由で通知を受け取り、
動画を処理してコーナー分類結果をCSVとしてS3に出力する。
"""

import os
import sys
from pathlib import Path

# torch/lib をDLLサーチパスに追加（PaddlePaddleとの競合回避）
_torch_lib = Path(sys.executable).parent.parent / "Lib" / "site-packages" / "torch" / "lib"
if _torch_lib.exists() and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(str(_torch_lib))

import csv
import io
import json
import logging
import tempfile
import time
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


def _calc_cost(usage: dict | None) -> dict:
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_jpy": 0.0}
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    total = usage.get("total_tokens", inp + out)
    thinking = max(0, total - inp - out)
    cost_usd = (
        inp * settings.gemini_input_price_usd_per_1m
        + out * settings.gemini_output_price_usd_per_1m
        + thinking * settings.gemini_thinking_price_usd_per_1m
    ) / 1_000_000
    cost_jpy = cost_usd * settings.usd_to_jpy
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "thinking_tokens": thinking,
        "total_tokens": total,
        "cost_usd": cost_usd,
        "cost_jpy": cost_jpy,
    }


def _corners_to_csv(filename: str, corners: list[dict], cost: dict) -> str:
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
    writer.writerow([])
    writer.writerow(["# Gemini使用トークン数"])
    writer.writerow(["入力トークン", "出力トークン", "思考トークン", "合計トークン", "料金(USD)", "料金(円・概算)"])
    writer.writerow([
        cost["input_tokens"],
        cost["output_tokens"],
        cost["thinking_tokens"],
        cost["total_tokens"],
        f"${cost['cost_usd']:.6f}",
        f"約{cost['cost_jpy']:.2f}円 (1USD={settings.usd_to_jpy}円換算)",
    ])
    return buf.getvalue()


def _build_output_key(s3_key: str, suffix: str = "_corners.csv") -> str:
    parts = s3_key.split("/")
    date_dir = parts[-2] if len(parts) >= 2 else "unknown"
    stem = Path(s3_key).stem
    base = settings.s3_output_prefix.rstrip("/")
    station = settings.s3_output_station.strip("/")
    return f"{base}/{station}/{date_dir}/{stem}{suffix}"


def _sec_to_hms(sec: float) -> str:
    h = int(sec) // 3600
    m = (int(sec) % 3600) // 60
    s = int(sec) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_html_report(filename: str, corners: list[dict], audio_rows: list[dict], ocr_rows: list[dict], cost: dict) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows_html = ""
    for c in corners:
        tags = c.get("tags", [])
        tags_str = "　".join(tags) if isinstance(tags, list) else str(tags)
        rows_html += f"""
        <tr>
          <td>{_sec_to_hms(c['start_sec'])}</td>
          <td>{_sec_to_hms(c['end_sec'])}</td>
          <td><strong>{esc(c.get('title',''))}</strong></td>
          <td>{esc(c.get('summary',''))}</td>
          <td class="tags">{esc(tags_str)}</td>
        </tr>"""

    audio_html = "".join(
        f"<tr><td>{_sec_to_hms(r['start_sec'])}</td><td>{_sec_to_hms(r['end_sec'])}</td><td>{esc(str(r['text']))}</td></tr>"
        for r in audio_rows
    )
    ocr_html = "".join(
        f"<tr><td>{_sec_to_hms(r['start_sec'])}</td><td>{_sec_to_hms(r['end_sec'])}</td><td>{esc(str(r['text']))}</td></tr>"
        for r in ocr_rows
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{esc(filename)}</title>
<style>
  body {{ font-family: sans-serif; margin: 20px; background: #f5f5f5; }}
  h1 {{ font-size: 1.2em; color: #333; }}
  h2 {{ font-size: 1em; margin-top: 2em; border-bottom: 2px solid #ccc; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; margin-top: 8px; }}
  th {{ background: #444; color: #fff; padding: 6px 10px; text-align: left; font-size: 0.85em; }}
  td {{ padding: 5px 10px; border-bottom: 1px solid #eee; font-size: 0.85em; vertical-align: top; }}
  tr:hover td {{ background: #f0f4ff; }}
  .tags {{ color: #666; font-size: 0.8em; }}
</style>
</head>
<body>
<h1>{esc(filename)}</h1>

<h2>コーナー一覧（AI要約）</h2>
<table>
<tr><th>開始</th><th>終了</th><th>タイトル</th><th>要約</th><th>タグ</th></tr>
{rows_html}
</table>

<h2>音声テキスト（Whisper）</h2>
<table>
<tr><th>開始</th><th>終了</th><th>テキスト</th></tr>
{audio_html}
</table>

<h2>テロップ（OCR）</h2>
<table>
<tr><th>開始</th><th>終了</th><th>テキスト</th></tr>
{ocr_html}
</table>

<h2>Gemini API 使用量（概算）</h2>
<table>
<tr><th>入力トークン</th><th>出力トークン</th><th>思考トークン</th><th>合計トークン</th><th>料金 (USD)</th><th>料金 (円)</th></tr>
<tr>
  <td>{cost["input_tokens"]:,}</td>
  <td>{cost["output_tokens"]:,}</td>
  <td>{cost["thinking_tokens"]:,}</td>
  <td>{cost["total_tokens"]:,}</td>
  <td>${cost["cost_usd"]:.6f}</td>
  <td>約 {cost["cost_jpy"]:.2f} 円<br><small>1USD={settings.usd_to_jpy}円換算・公式レートに基づく概算</small></td>
</tr>
</table>
</body>
</html>"""


from app.athena import sync_glue_table


def _process_s3_object(s3_key: str) -> None:
    boto_kwargs = _build_boto_kwargs()
    s3 = boto3.client("s3", **boto_kwargs)

    ext = Path(s3_key).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        log.info("スキップ（動画以外）: %s", s3_key)
        return

    log.info("処理開始: s3://%s/%s", settings.s3_bucket, s3_key)

    with tempfile.TemporaryDirectory(dir="A:\\tmp") as tmpdir:
        local_video = Path(tmpdir) / Path(s3_key).name
        log.info("ダウンロード中...")
        s3.download_file(settings.s3_bucket, s3_key, str(local_video))
        log.info("ダウンロード完了: %s", local_video)

        analyzed = segment_corners(local_video)

        corners = analyzed.get("corners", [])
        audio_rows = analyzed.get("audio_rows", [])
        ocr_rows = analyzed.get("ocr_rows", [])
        cost = _calc_cost(analyzed.get("gemini_usage"))
        log.info("コーナー数: %d  Gemini: %dトークン 約%.2f円", len(corners), cost["total_tokens"], cost["cost_jpy"])

        filename = Path(s3_key).name

        csv_key = _build_output_key(s3_key, "_corners.csv")
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=csv_key,
            Body=_corners_to_csv(filename, corners, cost).encode("utf-8-sig"),
            ContentType="text/csv; charset=utf-8",
        )
        log.info("CSV出力完了: s3://%s/%s", settings.s3_bucket, csv_key)

        html_key = _build_output_key(s3_key, "_report.html")
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=html_key,
            Body=_build_html_report(filename, corners, audio_rows, ocr_rows, cost).encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        log.info("HTML出力完了: s3://%s/%s", settings.s3_bucket, html_key)

        try:
            for i, corner in enumerate(corners):
                tags_str = "|".join(corner.get("tags", [])) if isinstance(corner.get("tags"), list) else str(corner.get("tags", ""))
                buf = io.StringIO()
                csv.writer(buf).writerow([
                    f"{corner['start_sec']:.3f}",
                    f"{corner['end_sec']:.3f}",
                    corner.get("title", ""),
                    corner.get("summary", ""),
                    tags_str,
                ])
                s3.put_object_annotation(
                    Bucket=settings.s3_bucket,
                    Key=s3_key,
                    AnnotationName=f"corner_{i:04d}",
                    AnnotationPayload=buf.getvalue().strip().encode("utf-8"),
                )
            log.info("アノテーション付与完了: %d件 s3://%s/%s", len(corners), settings.s3_bucket, s3_key)
            sync_glue_table()
        except Exception as e:
            log.warning("アノテーション付与失敗（boto3未対応の可能性）: %s", e)


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
