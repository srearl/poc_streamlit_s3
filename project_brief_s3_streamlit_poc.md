# Project Brief: Local Streamlit Editor for S3-Backed Master Matrix with Audit
# Logging

## 1. Project Objectives

We are building a file-based data editing system that:

-   Uses S3 as the authoritative data store.
-   Allows multiple researchers to edit a shared master matrix.
-   Preserves reproducibility through structured audit logging.
-   Avoids use of a traditional database.
-   Integrates with AWS infrastructure (S3 now, Athena/Glue later).
-   Runs locally but connects to AWS resources.
-   Is structured for development using Astral's `uv`.

------------------------------------------------------------------------

## 2. Architectural Constraints

-   No relational database backend allowed.
-   Authoritative state must reside in S3.
-   Users run the editing app locally.
-   Every modification must be logged.
-   System must support reproducibility via snapshots + versioning.
-   Must scale beyond the POC.
-   Development environment: VS Code + Astral `uv`.

------------------------------------------------------------------------

## 3. Current Dataset (POC)

Sample file: `master.csv`

-   125 rows
-   13 columns:
    -   vessel (string)
    -   cruise (string)
    -   tow (int)
    -   net (int)
    -   lat_decimal (float)
    -   long_decimal (float)
    -   min_depth (float)
    -   max_depth (float)
    -   volume_filtered (float)
    -   time_start_gmt (string)
    -   time_start_local (string)
    -   is_day (bool)
    -   net_size (float)

Potential logical key: `tow + net`

------------------------------------------------------------------------

## 4. Current S3 Architecture

Recommended bucket layout:

s3://my-bucket/ data/ master.csv \# authoritative file snapshots/
master_YYYYMMDDTHHMMSS_uuid.csv audit/ YYYY-MM-DD/
user_timestamp_uuid.jsonl

Optional (for Athena/Glue later): data/master.parquet and matching
snapshots with `.parquet` extension. The application defaults to Parquet;
switch to CSV only if you must edit the CSV master and then convert back to
Parquet.

Athena prerequisite
- Set the Athena workgroup query results location to an existing prefix, e.g.
    `s3://hypoxia24/athena-results/`. Create that prefix first and grant
    s3:PutObject/ListBucket so Athena can write results.

Terminology
- Master file: authoritative object edited by Streamlit (CSV or Parquet) with S3
VersionId used for optimistic locking.
- Snapshot prefix: per-save immutable copy of the master using the same
extension as the master; use to inspect/restore point-in-time states.
- Audit prefix: per-save JSONL entry (one line) with timestamp, user, note,
prev/new VersionIds, snapshot_key, row_count, column_count. Audits stay JSONL
regardless of master format.

### Behavior

1.  Authoritative File
    -   Overwritten on save.
    -   S3 Versioning enabled.
2.  Snapshots
    -   Immutable copy written on every save.
    -   Used for reproducibility and rollback.
3.  Audit Logging
    -   Small JSONL object per save.
    -   Stored per user per day.
    -   Contains:
        -   timestamp
        -   user
        -   reason
        -   prev_version
        -   new_version
        -   snapshot_key
        -   row/column counts
4.  Concurrency
    -   Optimistic locking via S3 VersionId.
    -   Abort save if version changed since load.

    ### SQL round-trip (default path)

    -   Build Parquet master from CSV via CTAS to `data/master.parquet` (empty
        the target prefix first). Athena: one statement per run; drop then
        create.
    -   Apply edits via CTAS rewrite to `data/master_next.parquet` (empty the
        target first). Example: use a CTE to compute new values and filter out
        rows to delete.
    -   Promote the edited data to the app key by copying the newest part or
        flattening all parts to the single object `data/master.parquet` (use
        `scripts/flatten_parquet_parts.py`).
    -   Set `S3_MASTER_KEY=data/master.parquet` and `S3_FILE_FORMAT=parquet` in
        the app, then reload. Export back to CSV if needed for delivery.
    -   Partition by cruise or vessel in CTAS tables if the dataset grows.

### Offline Behavior

-   If S3 is unreachable, saves are blocked to avoid divergence. The app
    shows an error and does not queue local writes. We can add a mock S3
    mode later for demos if needed.

------------------------------------------------------------------------

## 5. Technology Stack

### Runtime

-   Local Streamlit application
-   AWS S3 backend

### Python Packages

-   streamlit
-   st-aggrid
-   boto3
-   pandas

Future: - pyarrow (for Parquet + Athena performance)

### Development

-   Astral `uv` project scaffold
-   VS Code
-   AWS CLI for S3 operations

------------------------------------------------------------------------

## 6. Rationale

Why file-based? - Project requirement prohibits database. - S3 provides
durability + versioning. - Compatible with Athena/Glue. - Low
operational overhead.

Why AG Grid + Streamlit? - Spreadsheet-like editing. - Actively
maintained. - Python-native workflow.

Why per-save audit objects? - Avoid rewriting large audit logs. -
Scalable pattern. - Easy aggregation later.

------------------------------------------------------------------------

## 7. Next Development Steps

1.  Convert CSV to Parquet.
2.  Normalize datetime fields.
3.  Implement row-level diff logging.
4.  Partition data by cruise or vessel if scale increases (for SQL and
    Glue performance).
5.  Integrate Glue catalog.
6.  Add authentication (AWS SSO).
7.  Formalize uv packaging + deployment process.

Iceberg (optional, separate doc)
- Only needed if heavy MERGE/DELETE patterns or table-level time-travel/rollback
    become requirements. Otherwise, stay on the CSV/Parquet + snapshots + JSONL
    audits flow above. See [ICEBERG.md](ICEBERG.md) for details; always export
    back to the single-object master keys for the app.

------------------------------------------------------------------------

Generated on: 2026-02-12T16:59:14.307994Z
