"""Athena検索・Glue同期ユーティリティ"""

import logging
import time

import boto3

from app.config import settings

log = logging.getLogger(__name__)

_S3TABLES_BUCKET_ARN = "arn:aws:s3tables:ap-northeast-1:656445866169:bucket/aws-s3"
_GLUE_DB = "bangumi_annotations"
_GLUE_TABLE = "annotation"
ATHENA_OUTPUT = f"s3://{settings.s3_bucket}/athena-results/"


def boto_kwargs() -> dict:
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


def sync_glue_table() -> None:
    """S3 Tablesの最新metadata_locationをGlueテーブルに反映する。"""
    try:
        kw = boto_kwargs()
        s3tables = boto3.client("s3tables", **kw)
        meta = s3tables.get_table_metadata_location(
            tableBucketARN=_S3TABLES_BUCKET_ARN,
            namespace="b_bangumi-info",
            name="annotation",
        )
        new_location = meta["metadataLocation"]

        glue = boto3.client("glue", **kw)
        tbl = glue.get_table(DatabaseName=_GLUE_DB, Name=_GLUE_TABLE)["Table"]

        if tbl.get("Parameters", {}).get("metadata_location") == new_location:
            return

        tbl["Parameters"]["metadata_location"] = new_location
        for key in ("DatabaseName", "CreateTime", "UpdateTime", "CreatedBy",
                    "IsRegisteredWithLakeFormation", "CatalogId", "VersionId",
                    "IsMultiDialectView", "IsMaterializedView", "ViewDefinition",
                    "ViewExpandedText", "ViewOriginalText"):
            tbl.pop(key, None)

        glue.update_table(DatabaseName=_GLUE_DB, TableInput=tbl)
        log.info("Glueアノテーションテーブル更新完了: %s", new_location)
    except Exception as e:
        log.warning("Glueアノテーションテーブル更新失敗: %s", e)


def run_athena_query(sql: str) -> list[dict]:
    """Athenaクエリを実行し、結果を辞書のリストで返す。"""
    sync_glue_table()

    kw = boto_kwargs()
    athena = boto3.client("athena", **kw)
    resp = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": _GLUE_DB},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
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

    result = athena.get_query_results(QueryExecutionId=qid)
    rows = result["ResultSet"]["Rows"]
    if len(rows) <= 1:
        return []

    headers = [c["VarCharValue"] for c in rows[0]["Data"]]
    return [
        {headers[i]: col.get("VarCharValue", "") for i, col in enumerate(row["Data"])}
        for row in rows[1:]
    ]
