"""Local Streamlit editor for an S3-backed master matrix with audit logging."""

from __future__ import annotations

import io
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import json
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder


@dataclass
class S3Layout:
    bucket: str
    master_key: str
    snapshot_prefix: str
    audit_prefix: str
    profile: Optional[str]
    file_format: str  # csv or parquet


def get_boto3_client(profile: Optional[str]):
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return session.client("s3")
    except Exception as exc:  # boto3 raises various errors for profile issues
        raise RuntimeError(f"Unable to create boto3 client (profile={profile}): {exc}")


def load_dataset(client, layout: S3Layout):
    try:
        obj = client.get_object(Bucket=layout.bucket, Key=layout.master_key)
    except ClientError as exc:
        raise RuntimeError(f"Failed to load {layout.master_key} from S3: {exc}")

    version_id = obj.get("VersionId")
    raw = obj["Body"].read()

    try:
        if layout.file_format == "parquet":
            df = pd.read_parquet(io.BytesIO(raw))
        else:
            df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise RuntimeError(f"Failed to parse dataset: {exc}")

    _validate_dataset(df)
    return df, version_id


def head_version(client, layout: S3Layout) -> Optional[str]:
    try:
        head = client.head_object(Bucket=layout.bucket, Key=layout.master_key)
        return head.get("VersionId")
    except ClientError:
        return None


def save_dataset(client, layout: S3Layout, df: pd.DataFrame, expected_version: Optional[str], user_note: str):
    _validate_dataset(df)
    current_version = head_version(client, layout)
    if expected_version and current_version and current_version != expected_version:
        raise RuntimeError("Master file changed in S3 since you loaded it. Reload before saving.")

    buffer = io.BytesIO()
    if layout.file_format == "parquet":
        df.to_parquet(buffer, index=False)
        ext = "parquet"
    else:
        df.to_csv(buffer, index=False)
        ext = "csv"
    buffer.seek(0)

    blob = buffer.getvalue()

    try:
        put_resp = client.put_object(Bucket=layout.bucket, Key=layout.master_key, Body=blob)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to save master file: {exc}")

    new_version = put_resp.get("VersionId")
    snapshot_key = build_snapshot_key(layout.snapshot_prefix, ext)
    _write_snapshot(client, layout.bucket, snapshot_key, blob)
    audit_key = build_audit_key(layout.audit_prefix)
    _write_audit_entry(client, layout.bucket, audit_key, user_note, expected_version, new_version, snapshot_key, df)

    return new_version, snapshot_key, audit_key


def _write_snapshot(client, bucket: str, key: str, payload: bytes):
    try:
        client.put_object(Bucket=bucket, Key=key, Body=payload)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to write snapshot {key}: {exc}")


def _write_audit_entry(client, bucket: str, key: str, note: str, prev_version: Optional[str], new_version: Optional[str], snapshot_key: str, df: pd.DataFrame):
    ts = datetime.now(timezone.utc).isoformat()
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    entry = {
        "timestamp": ts,
        "user": user,
        "note": note,
        "prev_version": prev_version,
        "new_version": new_version,
        "snapshot_key": snapshot_key,
        "row_count": len(df.index),
        "column_count": len(df.columns),
    }
    payload = (json.dumps(entry) + "\n").encode()
    try:
        client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/jsonl")
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to write audit log {key}: {exc}")


def _validate_dataset(df: pd.DataFrame):
    if df.empty:
        raise ValueError("Dataset is empty; nothing to edit.")
    if {"tow", "net"}.issubset(df.columns):
        dupes = df[["tow", "net"]].duplicated().sum()
        if dupes:
            raise ValueError(f"Dataset has {dupes} duplicate tow+net combinations; resolve before editing.")


def build_snapshot_key(prefix: str, ext: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix.rstrip('/')}/master_{ts}_{uuid.uuid4().hex}.{ext}"


def build_audit_key(prefix: str) -> str:
    ts = datetime.now(timezone.utc)
    day = ts.strftime("%Y-%m-%d")
    stamp = ts.strftime("%Y%m%dT%H%M%S")
    return f"{prefix.rstrip('/')}/{day}/user_{stamp}_{uuid.uuid4().hex}.jsonl"


def render_grid(df: pd.DataFrame) -> pd.DataFrame:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(editable=True, filter=True, resizable=True)
    gb.configure_grid_options(domLayout="normal")
    grid = AgGrid(
        df,
        gridOptions=gb.build(),
        update_on=["cellValueChanged"],  # replaces deprecated GridUpdateMode
        fit_columns_on_grid_load=True,
        enable_enterprise_modules=False,
    )
    edited = grid["data"]
    return pd.DataFrame(edited)


def sidebar_config() -> S3Layout:
    st.sidebar.header("S3 Configuration")
    bucket = st.sidebar.text_input("Bucket", value=os.environ.get("S3_BUCKET", "my-bucket"))
    master_key = st.sidebar.text_input("Master key", value=os.environ.get("S3_MASTER_KEY", "data/master.csv"))
    snapshot_prefix = st.sidebar.text_input("Snapshot prefix", value=os.environ.get("S3_SNAPSHOT_PREFIX", "snapshots"))
    audit_prefix = st.sidebar.text_input("Audit prefix", value=os.environ.get("S3_AUDIT_PREFIX", "audit"))
    profile = st.sidebar.text_input("AWS profile", value=os.environ.get("AWS_PROFILE", "zoop"))
    file_format_default = os.environ.get("S3_FILE_FORMAT", "csv").lower()
    file_format_options = ["csv", "parquet"]
    file_format_index = file_format_options.index(file_format_default) if file_format_default in file_format_options else 1
    file_format = st.sidebar.selectbox("File format", options=file_format_options, index=file_format_index)
    st.sidebar.caption("Saves are blocked if S3 is unreachable to prevent divergence.")
    return S3Layout(bucket, master_key, snapshot_prefix, audit_prefix, profile, file_format)


def streamlit_app():
    st.set_page_config(page_title="S3 Master Editor", layout="wide")
    st.title("S3-backed Master Matrix Editor")

    layout = sidebar_config()

    try:
        client = get_boto3_client(layout.profile)
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    load_col, save_col = st.columns([1, 1])
    with load_col:
        if st.button("Reload from S3", type="primary") or "dataset" not in st.session_state:
            try:
                df, version_id = load_dataset(client, layout)
                st.session_state["dataset"] = df
                st.session_state["version_id"] = version_id
                st.success(f"Loaded {len(df)} rows (version {version_id}).")
            except Exception as exc:
                st.error(exc)
                st.stop()

    if "dataset" not in st.session_state:
        st.info("Load data to begin editing.")
        st.stop()

    df = st.session_state["dataset"]
    st.caption("Edit cells directly; tow+net should stay unique.")
    edited_df = render_grid(df)

    note = st.text_input("Save note (who/why)", placeholder="e.g., corrected depths for SR2407 tow 3")

    with save_col:
        if st.button("Save to S3", type="secondary"):
            try:
                new_version, snapshot_key, audit_key = save_dataset(
                    client, layout, edited_df, st.session_state.get("version_id"), note
                )
                st.session_state["dataset"] = edited_df
                st.session_state["version_id"] = new_version
                st.success(
                    f"Saved. New version {new_version}; snapshot {snapshot_key}; audit {audit_key}."
                )
            except Exception as exc:
                st.error(exc)


def main():
    streamlit_app()


if __name__ == "__main__":
    main()
