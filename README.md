## S3-backed Streamlit Editor

Local Streamlit app that edits an authoritative master file in S3 with
snapshots and per-save audit logs. No database required.

### Prerequisites
- Python 3.12+
- AWS CLI configured with profile `zoop` (or set `AWS_PROFILE`)
- S3 bucket with versioning enabled

### Minimum AWS permissions
- s3:GetObject/PutObject/HeadObject on the master key, snapshot prefix, and
audit prefix.
- s3:ListBucket on the bucket for smooth app reloads.
- For Athena/Glue work (optional, see below): Glue catalog read/write and S3
access to your Iceberg/CTAS locations and Athena query-results bucket.

### Setup
```bash
uv sync
```

Common environment for this project (adjust bucket/prefixes):
```bash
export AWS_PROFILE=zoop \
	S3_BUCKET=hypoxia24 \
	S3_MASTER_KEY=data/master.parquet \
	S3_SNAPSHOT_PREFIX=snapshots \
	S3_AUDIT_PREFIX=audit \
	S3_FILE_FORMAT=parquet
```

Athena prerequisite: set a query results location; create that prefix first
(empty folder is fine) and ensure the workgroup points to it. IAM needs
s3:PutObject/ListBucket on that prefix.

### Recommended workflow (Parquet-only)

1. Convert locally once:
<!-- `pandas.read_csv('master.csv').to_parquet('master.parquet', index=False)`. -->

```bash
uv run python - <<'PY'
import pandas as pd
df = pd.read_csv("master.csv")
df.to_parquet("master.parquet", index=False)
PY
```

2. Upload the Parquet master:
`aws s3 cp master.parquet s3://$S3_BUCKET/data/master.parquet --profile $AWS_PROFILE`.
3. Run the app with Parquet defaults:
`S3_FILE_FORMAT=parquet` and `S3_MASTER_KEY=data/master.parquet`.
4. Small edits: Streamlit directly on `data/master.parquet`; snapshots stay
Parquet; audits stay JSONL.
5. Bulk edits: Athena CTAS rewrite to a temp prefix, then flatten/copy back to
`data/master.parquet` (see below).
6. Delivery (only if needed as CSV): download the Parquet master and convert
locally to CSV, or run a one-off CTAS to CSV.

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
- Sidebar fields select bucket, master key (default `data/master.parquet`),
	snapshot prefix (`snapshots`), and audit prefix (`audit`).
	Format is Parquet-only in the app.
- Saves are blocked if S3 is unreachable; no local queueing.
- Tow + net should remain unique; resolve duplicates before saving.

### Prefixes and artifacts
- Master file: the authoritative Parquet object you edit. VersionId from S3 is
used for optimistic locking.
- Snapshot prefix: per-save immutable copy of the master in Parquet. Default is
	`s3://<bucket>/snapshots/` with names like
	`snapshot/master_<YYYYMMDDTHHMMSS>_<uuid>.parquet`. Use these to inspect or
	restore a point-in-time state (copy a chosen snapshot back over
	`data/master.parquet` if you need to roll back).
- Audit prefix: per-save JSONL record (one line) containing
	`timestamp,user,note,prev_version,new_version,snapshot_key,row_count,column_count`.
	Default is `s3://<bucket>/audit/` with per-day subfolders. Example to read the
	latest audit (Python helper, uses env/default creds):
	```bash
	uv run python - <<'PY'
	import boto3, os

	bucket = os.environ['S3_BUCKET']
	prefix = os.environ.get('S3_AUDIT_PREFIX', 'audit').rstrip('/') + '/'
	s3 = boto3.client('s3')
	resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
	items = resp.get('Contents', [])
	if not items:
	    raise SystemExit('No audit objects found')
	latest = sorted(items, key=lambda x: x['LastModified'])[-1]['Key']
	body = s3.get_object(Bucket=bucket, Key=latest)['Body'].read().decode()
	print(f"Latest audit: {latest}\n{body.strip().splitlines()[-1]}")
	PY
	```
	Field meanings: `timestamp` (UTC save time), `user` (OS user running the app),
	`note` (save note), `prev_version`/`new_version` (S3 VersionIds before/after),
	`snapshot_key` (Parquet snapshot for that save), `row_count`/`column_count`
	(shape at save time). Use `snapshot_key` to inspect or restore by copying it
	over `data/master.parquet`.
- Store prefixes under the same bucket; they default to `snapshots/` and `audit/`
	in the UI.

### Output
- Master file overwritten in-place (VersionId tracked for optimistic
	locking).
- Snapshot written per save under the snapshot prefix.
- JSONL audit log written per save under the audit prefix.

### Parquet rewrite workflow (Athena → Streamlit)
Use this when you want to edit via SQL and keep the app pointed at a single
Parquet object.

1) Define an external table on the Parquet master (schema declared once):
	```sql
	DROP TABLE IF EXISTS work_master_parquet;
	CREATE EXTERNAL TABLE work_master_parquet (
	  vessel string,
	  cruise string,
	  tow int,
	  net int,
	  lat_decimal double,
	  long_decimal double,
	  min_depth double,
	  max_depth double,
	  volume_filtered double,
	  time_start_gmt string,
	  time_start_local string,
	  is_day boolean,
	  net_size double
	)
	STORED AS PARQUET
	LOCATION 's3://hypoxia24/data/master.parquet';
	```

2) Apply edits via CTAS rewrite to a temp prefix (example update/delete for tow=4):
	```sql
	DROP TABLE IF EXISTS work_master_parquet_tmp;
	CREATE TABLE work_master_parquet_tmp
	WITH (
	  external_location = 's3://hypoxia24/data/master_next.parquet',
	  format = 'PARQUET',
	  write_compression = 'SNAPPY'
	) AS
	WITH s AS (
	  SELECT tow, net, LAG(volume_filtered) OVER (PARTITION BY tow ORDER BY net) AS new_volume
	  FROM work_master_parquet
	  WHERE tow = 4
	)
	SELECT
	  m.vessel, m.cruise, m.tow, m.net,
	  m.lat_decimal, m.long_decimal, m.min_depth, m.max_depth,
	  CASE WHEN m.tow = 4 AND s.new_volume IS NOT NULL THEN s.new_volume ELSE m.volume_filtered END AS volume_filtered,
	  m.time_start_gmt, m.time_start_local, m.is_day, m.net_size
	FROM work_master_parquet m
	LEFT JOIN s ON m.tow = s.tow AND m.net = s.net
	WHERE NOT (m.tow = 4 AND m.net = 0);
	```

3) Promote the edited data (result must be exactly one object at `data/master.parquet`):
	- Preferred: flatten all parts with the helper script (handles part files
	without `.parquet` suffixes):
		```bash
		uv run python scripts/flatten_parquet_parts.py \
		  --bucket hypoxia24 \
		  --prefix data/master_next.parquet/ \
		  --output-key data/master.parquet \
		  --profile zoop
		```
	- Tiny datasets: copy a single part (never `_SUCCESS`, never the whole prefix):
		```bash
		latest_part=$(aws s3 ls s3://hypoxia24/data/master_next.parquet/ --profile zoop \
		  | sort -k1,2 \
		  | tail -n1 | awk '{print $4}')
		aws s3 cp \
		  s3://hypoxia24/data/master_next.parquet/$latest_part \
		  s3://hypoxia24/data/master.parquet \
		  --profile zoop
		```

4) Verify and reload:
	```bash
	aws s3 cp s3://hypoxia24/data/master.parquet /tmp/master.parquet --profile zoop
	uv run python - <<'PY'
	import pandas as pd
	print(pd.read_parquet('/tmp/master.parquet').head())
	PY
	```
	Set `S3_MASTER_KEY=data/master.parquet` and `S3_FILE_FORMAT=parquet` in the
	app, then click Reload.

Notes:
- Always empty target prefixes before CTAS (`aws s3 rm s3://… --recursive`).
- Athena allows one statement per run; drop and create are separate.
- If you see “magic bytes” errors, the app is parsing a non-Parquet
object—ensure the master key and format match and that `data/master.parquet` is
the flattened object.

### Export back to CSV (optional)
	```sql
	CREATE TABLE export_master_csv
	WITH (
	  external_location='s3://hypoxia24/data/master.csv',
	  format='TEXTFILE'
	) AS
	SELECT * FROM work_master_parquet;
	```

Switch the app to the format you are actively editing: `S3_FILE_FORMAT=parquet`
with `data/master.parquet` during Parquet cycles; back to CSV when you overwrite
the CSV master. Partition by cruise or vessel in these tables if the dataset
grows to speed up SQL.


### Iceberg (optional, separate doc)
Iceberg is not required for this workflow. If you need ACID MERGE/DELETE or
time-travel, see [ICEBERG.md](ICEBERG.md) for an Athena engine v3 example and
remember to export back to `data/master.parquet` or `data/master.csv` for the
app.

### Pruning and archiving (manual)
- Keep the latest 15 Streamlit saves (snapshots + audits) per master key.
- Move older Streamlit artifacts to `archive/streamlit/<YYYYMMDD>/` using
filenames like `snapshot_<VersionId>.parquet` and `audit_<VersionId>.jsonl` (or
timestamp + short UUID if VersionId is unavailable).
- Glue job logs/manifests are not pruned; keep all under your `audit/glue/`
path.

### Next steps / automation ideas

- Triggered flatten/promotion: Lambda or Glue Python shell that lists
	`data/master_next.parquet/`, merges parts, and overwrites
	`data/master.parquet`; invoke via EventBridge (schedule) or Athena query
	completion.
- Workflow orchestration: Step Functions wrapping “Athena CTAS → flatten/copy →
notify”.
- Local/CI helper: add a task/Make target that runs
	`uv run python scripts/flatten_parquet_parts.py --bucket $S3_BUCKET --prefix data/master_next.parquet/ --output-key data/master.parquet`.