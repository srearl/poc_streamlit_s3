import argparse
import io
import sys
from typing import List

import boto3
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten Parquet part files under a prefix into a single Parquet object."
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name, e.g. hypoxia24")
    parser.add_argument(
        "--prefix",
        required=True,
        help="Prefix that holds the part files, e.g. data/master.parquet/",
    )
    parser.add_argument(
        "--output-key",
        required=True,
        help="Full object key to write the flattened Parquet, e.g. data/master.parquet",
    )
    parser.add_argument(
        "--profile",
        help="Optional AWS profile (falls back to default profile/credentials)",
    )
    return parser.parse_args()


def get_s3_client(profile: str | None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3")


def list_parquet_parts(client, bucket: str, prefix: str) -> List[str]:
    # Single listing is fine for this dataset size. Extend to pagination if needed.
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = resp.get("Contents", [])
    # Athena CTAS sometimes writes objects without a .parquet suffix. Include any
    # non-directory object under the prefix that is not a marker like _SUCCESS.
    return [
        obj["Key"]
        for obj in contents
        if not obj["Key"].endswith("/") and "_SUCCESS" not in obj["Key"]
    ]


def flatten_parts(client, bucket: str, keys: List[str], output_key: str):
    frames = []
    for key in keys:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        frames.append(pd.read_parquet(io.BytesIO(body)))
    if not frames:
        raise SystemExit("No Parquet parts found to flatten.")
    combined = pd.concat(frames, ignore_index=True)
    buf = io.BytesIO()
    combined.to_parquet(buf, index=False)
    buf.seek(0)
    client.put_object(Bucket=bucket, Key=output_key, Body=buf.getvalue())
    print(f"Wrote {output_key} from {len(keys)} part file(s); rows={len(combined.index)}")


def main():
    args = parse_args()
    client = get_s3_client(args.profile)
    keys = list_parquet_parts(client, args.bucket, args.prefix)
    if not keys:
        raise SystemExit(f"No .parquet parts found under {args.prefix}")
    flatten_parts(client, args.bucket, keys, args.output_key)


if __name__ == "__main__":
    main()
