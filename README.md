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
	S3_AUDIT_PREFIX=audit
```

### Run
```bash
# sanity check AWS access
aws s3 ls --profile zoop

# launch app (uses main.py)
uv run streamlit run main.py
```

If the UI fails to load data, verify the master object exists:
```bash
aws s3 cp master.csv s3://$S3_BUCKET/$S3_MASTER_KEY --profile $AWS_PROFILE
aws s3api head-object --bucket $S3_BUCKET --key $S3_MASTER_KEY --profile $AWS_PROFILE
```

### Configuration
- Sidebar fields select bucket, master key (default `data/master.csv`),
	snapshot prefix (`snapshots`), and audit prefix (`audit`).
- Saves are blocked if S3 is unreachable; no local queueing.
- Tow + net should remain unique; resolve duplicates before saving.

### Output
- Master file overwritten in-place (VersionId tracked for optimistic
	locking).
- Snapshot written per save under the snapshot prefix.
- JSONL audit log written per save under the audit prefix.

### Bulk edits outside Streamlit
- R via aws.s3:
	```r
	library(aws.s3)
	df <- s3read_using(read.csv, object="data/master.csv", bucket="hypoxia24")
	# ...edit df...
	s3write_using(df, write.csv, object="data/master.csv", bucket="hypoxia24", row.names=FALSE)
	```
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
