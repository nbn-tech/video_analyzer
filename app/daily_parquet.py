import argparse
import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import boto3
from botocore.exceptions import ClientError


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SCHEMA = pa.schema(
    [
        ("filename", pa.string()),
        ("program_start_time", pa.string()),
        ("program_start_sec", pa.int32()),
        ("start_sec", pa.float64()),
        ("end_sec", pa.float64()),
        ("title", pa.string()),
        ("summary", pa.string()),
        ("tags", pa.string()),
    ]
)


def _program_start(filename: str) -> tuple[str, int]:
    match = re.search(r"_(\d{8})_(\d{6})(?:\D|$)", filename)
    hhmmss = match.group(2) if match else "000000"
    hour = int(hhmmss[0:2])
    minute = int(hhmmss[2:4])
    second = int(hhmmss[4:6])
    return f"{hour:02d}:{minute:02d}:{second:02d}", hour * 3600 + minute * 60 + second


def _partition_values(s3_key: str) -> tuple[str, str, str]:
    parts = s3_key.split("/")
    if len(parts) < 4:
        raise ValueError(f"Unexpected input key layout: {s3_key}")
    channel = parts[1].lower()
    broadcast_date = parts[2]
    parsed_date = datetime.strptime(broadcast_date, "%Y%m%d")
    return channel, broadcast_date, _WEEKDAYS[parsed_date.weekday()]


def _daily_key(channel: str, broadcast_date: str, day_of_week: str) -> str:
    return (
        f"athena-data/channel={channel}/day_of_week={day_of_week}/"
        f"broadcast_date={broadcast_date}/corners.parquet"
    )


def update_daily_parquet(s3, bucket: str, s3_key: str, corners: list[dict]) -> tuple[str, int]:
    """Create or replace a channel/day Parquet file with one video's corners."""
    channel, broadcast_date, day_of_week = _partition_values(s3_key)
    parquet_key = _daily_key(channel, broadcast_date, day_of_week)
    filename = Path(s3_key).name
    program_start_time, program_start_sec = _program_start(filename)

    existing_rows: list[dict] = []
    try:
        body = s3.get_object(Bucket=bucket, Key=parquet_key)["Body"].read()
        existing_rows = pq.read_table(io.BytesIO(body), schema=_SCHEMA).to_pylist()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in {"NoSuchKey", "404"}:
            raise

    # Reprocessing the same video replaces its previous rows instead of duplicating them.
    rows = [row for row in existing_rows if row.get("filename") != filename]
    for corner in corners:
        tags = corner.get("tags", [])
        rows.append(
            {
                "filename": filename,
                "program_start_time": program_start_time,
                "program_start_sec": program_start_sec,
                "start_sec": float(corner["start_sec"]),
                "end_sec": float(corner["end_sec"]),
                "title": str(corner.get("title", "")),
                "summary": str(corner.get("summary", "")),
                "tags": "|".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags),
            }
        )

    rows.sort(key=lambda row: (row["program_start_sec"], row["start_sec"], row["end_sec"]))
    output = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=_SCHEMA), output, compression="snappy")
    s3.put_object(
        Bucket=bucket,
        Key=parquet_key,
        Body=output.getvalue(),
        ContentType="application/vnd.apache.parquet",
    )
    return parquet_key, len(rows)


def update_daily_parquet_from_csv(
    bucket: str,
    source_key: str,
    csv_key: str,
    profile: str | None = None,
    region: str = "ap-northeast-1",
) -> tuple[str, int]:
    session = boto3.Session(profile_name=profile or None, region_name=region)
    s3 = session.client("s3")
    body = s3.get_object(Bucket=bucket, Key=csv_key)["Body"].read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(body)))
    corners = [
        {
            "start_sec": float(row["start_sec"]),
            "end_sec": float(row["end_sec"]),
            "title": row["title"],
            "summary": row["summary"],
            "tags": row["tags"].split("|") if row["tags"] else [],
        }
        for row in rows
    ]
    return update_daily_parquet(s3, bucket, source_key, corners)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update one channel/day Parquet from a result CSV.")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--csv-key", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-northeast-1")
    args = parser.parse_args()
    parquet_key, row_count = update_daily_parquet_from_csv(
        args.bucket,
        args.source_key,
        args.csv_key,
        args.profile,
        args.region,
    )
    print(json.dumps({"parquet_key": parquet_key, "row_count": row_count}))


if __name__ == "__main__":
    main()
