## S3-backed Streamlit Editor

Local Streamlit app that edits an authoritative master file in S3 with
snapshots and per-save audit logs. No database required.

### Prerequisites
- Python 3.12+
- AWS CLI configured with profile `zoop` (or set `AWS_PROFILE`)
- S3 bucket with versioning enabled

### Minimum AWS permissions
- s3:GetObject/PutObject/HeadObject on the master key, snapshot prefix, and audit prefix.
- s3:ListBucket on the bucket for smooth app reloads.
- For Athena/Glue work (optional, see below): Glue catalog read/write and S3 access to your Iceberg/CTAS locations and Athena query-results bucket.

### Setup
```bash
uv sync
```

Common environment for this project (adjust bucket/prefixes):
```bash
export AWS_PROFILE=zoop \
	S3_BUCKET=hypoxia24 \
	S3_MASTER_KEY=data/master.csv \
	S3_SNAPSHOT_PREFIX=snapshots \
	S3_AUDIT_PREFIX=audit \
	S3_FILE_FORMAT=csv
```

### Recommended workflow (CSV in → Parquet edits → CSV out)

1) Ingest: upload source CSV to `data/master.csv` (authoritative ingest artifact).
2) Convert for editing (optional but recommended): CTAS/convert to Parquet at `data/master.parquet` for faster Glue/Athena + Streamlit. When you do this, set `S3_MASTER_KEY=data/master.parquet` and `S3_FILE_FORMAT=parquet` in the app.
3) Bulk edits: run Glue/Athena on your chosen master (CSV or Parquet). Prefer Parquet for performance.
4) Small edits: use Streamlit against the same format you chose (CSV or Parquet). Snapshots follow that format; audits stay JSONL.
5) Deliver: export/CTAS back to CSV (e.g., `data/master_final.csv`). Parquet can remain the operational source if you used it.

### Keeping CSV and Parquet in sync

- Edit only one format at a time. During an edit cycle, consider that format the working master.
- Forward sync (CSV → Parquet): after ingesting a new CSV, overwrite the Parquet copy with a CTAS/export from the CSV-based table. Example CTAS is in the bulk-edits section below.
- Back sync (Parquet → CSV): when edits on Parquet are finished, CTAS/export the Parquet master back to CSV (overwrite `data/master.csv` or write `data/master_final.csv` for delivery).
- Switch the app to match the format you are editing: `S3_FILE_FORMAT=csv` + `S3_MASTER_KEY=data/master.csv` for CSV; `S3_FILE_FORMAT=parquet` + `S3_MASTER_KEY=data/master.parquet` for Parquet.

### Iceberg (optional)

- You can skip Iceberg and edit directly on Parquet (preferred for performance) or CSV. This is simplest for small datasets.
- Use Iceberg only if you need ACID MERGE/DELETE support, time-travel, or rollback. If you use it, rebuild `iceberg_master` from the current master (CSV or Parquet), run updates there, then CTAS/export back to your working master keys (`data/master.parquet` and/or `data/master.csv`).
- If you are not using Iceberg, you can delete `s3://$S3_BUCKET/iceberg/master/` after confirming you have exported a clean master.

### Run
```bash
# sanity check AWS access
aws s3 ls --profile zoop

# launch app (uses main.py)
uv run streamlit run main.py
```

If the UI fails to load data, verify the master object exists:
```bash
aws s3api head-object --bucket $S3_BUCKET --key $S3_MASTER_KEY --profile $AWS_PROFILE
```

### Configuration
- Sidebar fields select bucket, master key (default `data/master.csv`),
	snapshot prefix (`snapshots`), and audit prefix (`audit`).
	File format defaults to CSV (`S3_FILE_FORMAT=csv`); switch to Parquet when
	you have written `data/master.parquet` and want faster Glue/Athena/Streamlit.
- Saves are blocked if S3 is unreachable; no local queueing.
- Tow + net should remain unique; resolve duplicates before saving.

### Output
- Master file overwritten in-place (VersionId tracked for optimistic
	locking).
- Snapshot written per save under the snapshot prefix.
- JSONL audit log written per save under the audit prefix.

### Bulk edits outside Streamlit (Athena/Glue)
- Athena + Iceberg (Athena engine v3):
	- Rebuild Iceberg from raw CSV:
		```sql
		DROP TABLE IF EXISTS iceberg_master;
		-- clear old location in S3 if needed
		CREATE TABLE iceberg_master
		WITH (
			table_type='ICEBERG',
			format='parquet',
			location='s3://hypoxia24/iceberg/master/'
		) AS
		SELECT * FROM raw_master; -- raw_master is the external table over data/master.csv
		```
	- Update tow 4 volume_filtered and delete net 0 safely:
		```sql
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
		```
	- Check snapshot history for rollback:
		```sql
		SELECT snapshot_id, made_current_at FROM "iceberg_master$history" ORDER BY made_current_at DESC LIMIT 5;
		```
	- Export back to S3 object for the app (if you switch formats):
		```sql
		CREATE TABLE export_master
		WITH (
			external_location='s3://hypoxia24/data/master.parquet',
			format='parquet',
			write_compression='SNAPPY'
		) AS
		SELECT * FROM iceberg_master;
		```
		Then set Streamlit file format to Parquet and master key to `data/master.parquet`.

	- Export Parquet back to CSV for delivery (or to reset CSV ingest):
		```sql
		CREATE TABLE export_master_csv
		WITH (
			external_location='s3://hypoxia24/data/master.csv',
			format='TEXTFILE'
		) AS
		SELECT * FROM iceberg_master;
		```
		If you prefer a separate delivery object, use `data/master_final.csv` instead of `data/master.csv`.

### Pruning and archiving (manual)
- Keep the latest 15 Streamlit saves (snapshots + audits) per master key.
- Move older Streamlit artifacts to `archive/streamlit/<YYYYMMDD>/` using filenames like `snapshot_<VersionId>.parquet` and `audit_<VersionId>.jsonl` (or timestamp + short UUID if VersionId is unavailable).
- Glue job logs/manifests are not pruned; keep all under your `audit/glue/` path.
