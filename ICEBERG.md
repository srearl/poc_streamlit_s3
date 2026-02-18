# Iceberg (Optional)

Use Iceberg only if you need ACID MERGE/DELETE and table-level
time-travel/rollback. The Streamlit app still reads a single-object master (CSV
or Parquet), so always export from Iceberg back to `data/master.parquet` or
`data/master.csv` when done.

## When to consider
- Heavy MERGE/DELETE/UPDATE workloads that are awkward with CTAS rewrites.
- Need for time-travel/rollback at the table layer.
- Multiple engines (Athena/Trino/Spark) sharing an Iceberg table.

## Minimal Athena flow (engine v3)
```sql
-- Rebuild Iceberg from the CSV external table
DROP TABLE IF EXISTS iceberg_master;
CREATE TABLE iceberg_master
WITH (
  table_type='ICEBERG',
  format='parquet',
  location='s3://hypoxia24/iceberg/master/'
) AS
SELECT * FROM raw_master;

-- Example update/delete pattern
WITH s AS (
  SELECT tow, net,
         LAG(volume_filtered) OVER (PARTITION BY tow ORDER BY net) AS new_volume
  FROM iceberg_master
  WHERE tow = 4
)
INSERT OVERWRITE iceberg_master
SELECT
  m.vessel, m.cruise, m.tow, m.net,
  m.lat_decimal, m.long_decimal, m.min_depth, m.max_depth,
  CASE WHEN m.tow = 4 AND s.new_volume IS NOT NULL THEN s.new_volume ELSE m.volume_filtered END AS volume_filtered,
  m.time_start_gmt, m.time_start_local, m.is_day, m.net_size
FROM iceberg_master m
LEFT JOIN s ON m.tow = s.tow AND m.net = s.net
WHERE NOT (m.tow = 4 AND m.net = 0);

DELETE FROM iceberg_master WHERE tow = 4 AND net = 0;

-- Check history
SELECT snapshot_id, made_current_at
FROM "iceberg_master$history"
ORDER BY made_current_at DESC
LIMIT 5;

-- Export back to Parquet object for the app
CREATE TABLE export_master
WITH (
  external_location='s3://hypoxia24/data/master.parquet',
  format='parquet',
  write_compression='SNAPPY'
) AS
SELECT * FROM iceberg_master;
```

## Notes
- Always ensure the `data/master.parquet` or `data/master.csv` object is
refreshed after Iceberg edits before reloading the app.
- If you are not using Iceberg, you can delete `s3://hypoxia24/iceberg/master/`.
- Keep Athena query results and Iceberg locations separate from app
snapshots/audits.