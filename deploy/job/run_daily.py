#!/usr/bin/env python3
"""The daily update, run as the Fargate task's entry point.

1. Download the database from S3 (s3://$DATA_BUCKET/$DB_KEY).
2. `stratlib backfill`, then `stratlib screen --strategy <id>` for every strategy.
3. Check the database and upload it back to S3 (the bucket is versioned, so the previous copy stays recoverable).
4. Optionally publish the public snapshot and have the web instance install it (PUBLISH_BUCKET and PUBLISH_INSTANCE_ID).

A failed screen does not stop the others. A failed backfill skips the screens, since they would run on stale prices.
The task exits non-zero if any step failed, which raises the alert Terraform wires up. Progress is saved either way:
the backfill resumes per symbol, so a retry only pays for what is missing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig

APP = Path(os.environ.get("STRATLIB_DIR", "/app"))
DB = APP / "data" / "stratlib.db"
BUCKET = os.environ["DATA_BUCKET"]
KEY = os.environ.get("DB_KEY", "db/stratlib.db")
WORKERS = os.environ.get("WORKERS", "4")
STEP_TIMEOUT = int(os.environ.get("STEP_TIMEOUT_SECONDS", "5400"))
PUBLISH_BUCKET = os.environ.get("PUBLISH_BUCKET", "")
PUBLISH_INSTANCE = os.environ.get("PUBLISH_INSTANCE_ID", "")

log = logging.getLogger("daily")
S3_TRANSFER = TransferConfig(multipart_threshold=64 * 2**20, multipart_chunksize=64 * 2**20, max_concurrency=16)


def run(name: str, args: list[str]) -> bool:
    """Run one stratlib command; True if it exited 0. Its output goes straight to the task log."""
    log.info("== %s", name)
    started = time.monotonic()
    try:
        code = subprocess.run(["stratlib", *args], cwd=APP, timeout=STEP_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        log.error("%s did not finish within %d seconds", name, STEP_TIMEOUT)
        return False
    log.info("%s %s after %.0f s", name, "finished" if code == 0 else f"FAILED (exit code {code})", time.monotonic() - started)
    return code == 0


def strategy_ids() -> list[str]:
    from stratlib.strategies import STRATEGIES   # CANSLIM first, Trend Leaders next, then the scans
    return list(STRATEGIES)


def download(s3) -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = s3.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"]
    except s3.exceptions.ClientError as exc:
        raise SystemExit(f"No database at s3://{BUCKET}/{KEY} ({exc}). Seed it from the PC with deploy/db-sync.ps1 -Direction push.")
    free = shutil.disk_usage(DB.parent).free
    if free < size * 1.5:
        raise SystemExit(f"Only {free / 2**30:.1f} GiB free for a {size / 2**30:.1f} GiB database; raise the task's ephemeral storage.")
    log.info("Downloading s3://%s/%s (%.1f GiB)", BUCKET, KEY, size / 2**30)
    started = time.monotonic()
    s3.download_file(BUCKET, KEY, str(DB), Config=S3_TRANSFER)
    log.info("Downloaded in %.0f s", time.monotonic() - started)


def seal() -> None:
    """Fold the write-ahead log into the file and check it, so the one file uploaded is the whole database."""
    conn = sqlite3.connect(DB)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        (result,) = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if result != "ok":
        raise SystemExit(f"The database failed its integrity check ({result}); not uploading it. The S3 copy is unchanged.")


def upload(s3, path: Path, bucket: str, key: str) -> None:
    log.info("Uploading %s (%.1f GiB) to s3://%s/%s", path.name, path.stat().st_size / 2**30, bucket, key)
    started = time.monotonic()
    s3.upload_file(str(path), bucket, key, Config=S3_TRANSFER)
    log.info("Uploaded in %.0f s", time.monotonic() - started)


def publish(s3) -> bool:
    """Write the public snapshot, upload it to the release bucket and have the web instance install it."""
    snapshot = APP / "public" / "stratlib.db"
    if not run("publish", ["publish", "--output", str(snapshot)]):
        return False
    upload(s3, snapshot, PUBLISH_BUCKET, "release/stratlib.db")
    ssm = boto3.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[PUBLISH_INSTANCE], DocumentName="AWS-RunShellScript", Comment="stratlib daily refresh",
        Parameters={"commands": [f"/usr/local/sbin/stratlib-refresh {PUBLISH_BUCKET}"], "executionTimeout": ["1800"]},
    )["Command"]["CommandId"]
    log.info("Refresh command %s sent to %s", command_id, PUBLISH_INSTANCE)
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        time.sleep(10)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=PUBLISH_INSTANCE)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            log.info("Refresh %s\n%s", result["Status"], result.get("StandardOutputContent", ""))
            if result["Status"] != "Success":
                log.error("%s", result.get("StandardErrorContent", ""))
            return result["Status"] == "Success"
    log.error("The refresh did not finish within 20 minutes")
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    started = time.monotonic()
    s3 = boto3.client("s3")
    download(s3)

    failed: list[str] = []
    if run("backfill", ["backfill"]):
        for strategy_id in strategy_ids():
            if not run(f"screen {strategy_id}", ["screen", "--strategy", strategy_id, "--workers", WORKERS]):
                failed.append(f"screen {strategy_id}")
    else:
        failed.append("backfill (screens skipped)")

    seal()
    upload(s3, DB, BUCKET, KEY)

    if PUBLISH_BUCKET and PUBLISH_INSTANCE:
        if not publish(s3):
            failed.append("publish")

    summary = {"elapsed_seconds": round(time.monotonic() - started), "failed": failed}
    log.info("Summary: %s", json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
