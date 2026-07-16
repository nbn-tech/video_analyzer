CREATE DATABASE IF NOT EXISTS video_analyzer;

CREATE EXTERNAL TABLE IF NOT EXISTS video_analyzer.corner_csv (
    broadcast_date string,
    channel string,
    filename string,
    start_sec string,
    end_sec string,
    title string,
    summary string,
    tags string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES (
    'separatorChar' = ',',
    'quoteChar' = '"',
    'escapeChar' = '\\'
)
STORED AS TEXTFILE
LOCATION 's3://bangumi-info/results/'
TBLPROPERTIES ('skip.header.line.count' = '1');

ALTER TABLE video_analyzer.corner_csv
SET LOCATION 's3://bangumi-info/results/';

CREATE OR REPLACE VIEW video_analyzer.corners AS
SELECT
    broadcast_date,
    channel,
    filename,
    try_cast(start_sec AS double) AS start_sec,
    try_cast(end_sec AS double) AS end_sec,
    title,
    summary,
    tags
FROM video_analyzer.corner_csv
WHERE "$path" LIKE '%_corners.csv'
  AND regexp_like(broadcast_date, '^[0-9]{8}$')
  AND regexp_like(channel, '(?i)^ch[0-9]+$')
  AND try_cast(start_sec AS double) IS NOT NULL
  AND try_cast(end_sec AS double) IS NOT NULL;

DROP TABLE IF EXISTS video_analyzer.corner_csv_legacy;

CREATE EXTERNAL TABLE IF NOT EXISTS video_analyzer.daily_corners (
    filename string,
    program_start_time string,
    program_start_sec int,
    start_sec double,
    end_sec double,
    title string,
    summary string,
    tags string
)
PARTITIONED BY (
    channel string,
    day_of_week string,
    broadcast_date string
)
STORED AS PARQUET
LOCATION 's3://bangumi-info/athena-data/'
TBLPROPERTIES (
    'projection.enabled' = 'true',
    'projection.channel.type' = 'injected',
    'projection.day_of_week.type' = 'enum',
    'projection.day_of_week.values' = 'mon,tue,wed,thu,fri,sat,sun',
    'projection.broadcast_date.type' = 'date',
    'projection.broadcast_date.format' = 'yyyyMMdd',
    'projection.broadcast_date.range' = '20260101,NOW',
    'projection.broadcast_date.interval' = '1',
    'projection.broadcast_date.interval.unit' = 'DAYS',
    'storage.location.template' = 's3://bangumi-info/athena-data/channel=${channel}/day_of_week=${day_of_week}/broadcast_date=${broadcast_date}/'
);
