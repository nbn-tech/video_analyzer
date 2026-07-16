"""Athena query utility."""

import logging
import time

import boto3

from app.config import settings

log = logging.getLogger(__name__)

ATHENA_OUTPUT = f"s3://{settings.s3_bucket}/athena-results/"


def boto_kwargs() -> dict:
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


def boto_session() -> boto3.Session:
    if settings.athena_aws_profile:
        return boto3.Session(
            profile_name=settings.athena_aws_profile,
            region_name=settings.aws_region,
        )
    return boto3.Session(**boto_kwargs())


def run_athena_query(sql: str) -> list[dict]:
    """Athenaクエリを実行し、結果を辞書のリストで返す。"""
    athena = boto_session().client("athena")
    output_location = settings.athena_output_location or ATHENA_OUTPUT
    resp = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": settings.athena_glue_db},
        ResultConfiguration={"OutputLocation": output_location},
    )
    qid = resp["QueryExecutionId"]

    for _ in range(60):
        time.sleep(1)
        status = athena.get_query_execution(QueryExecutionId=qid)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athenaクエリ失敗: {reason}")

    rows: list[dict] = []
    token = None
    while True:
        args = {"QueryExecutionId": qid}
        if token:
            args["NextToken"] = token
        result = athena.get_query_results(**args)
        rows.extend(result["ResultSet"]["Rows"])
        token = result.get("NextToken")
        if not token:
            break
    if len(rows) <= 1:
        return []

    headers = [c["VarCharValue"] for c in rows[0]["Data"]]
    return [
        {headers[i]: col.get("VarCharValue", "") for i, col in enumerate(row["Data"])}
        for row in rows[1:]
    ]
