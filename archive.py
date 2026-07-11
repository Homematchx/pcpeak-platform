#!/usr/bin/env python3
"""
Append-only raw-capture archive -> durable S3-compatible object storage.

The raw source corpus (petition PDFs, docket, extraction + enrichment snapshots)
IS the moat: capture it raw and timestamped so it can be re-mined forever as the
models improve, and keep it off the local laptop (a disposable cache). Every
scrape writes a NEW timestamped snapshot under raw/{case}/{ts}/ — never
overwritten — so you also get a point-in-time history no competitor can rebuild.

Storage-agnostic (S3 API): works with Cloudflare R2, Backblaze B2, or AWS S3.

Config via env (ALL required to enable; if any is missing, archiving is a no-op
and the scraper behaves exactly as before):
    ARCHIVE_ENDPOINT           e.g. https://<accountid>.r2.cloudflarestorage.com
    ARCHIVE_BUCKET             bucket name
    ARCHIVE_ACCESS_KEY_ID
    ARCHIVE_SECRET_ACCESS_KEY
    ARCHIVE_REGION             optional, default "auto" (R2) or your S3 region

    python3 archive.py             # self-test: write+read+delete a probe object
"""
import os
import mimetypes

_REQUIRED = ("ARCHIVE_ENDPOINT", "ARCHIVE_BUCKET",
             "ARCHIVE_ACCESS_KEY_ID", "ARCHIVE_SECRET_ACCESS_KEY")


def archiving_enabled():
    return all(os.environ.get(k) for k in _REQUIRED)


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["ARCHIVE_ENDPOINT"],
        aws_access_key_id=os.environ["ARCHIVE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["ARCHIVE_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("ARCHIVE_REGION", "auto"),
        config=Config(retries={"max_attempts": 3, "mode": "standard"},
                      signature_version="s3v4"),
    )


def archive_case(case_number, snapshot_ts, files=None, blobs=None):
    """Upload a case's raw artifacts under raw/{case}/{snapshot_ts}/ (append-only).

      files : {name: local_path}          — uploaded if the path exists
      blobs : {name: (bytes, content_type)} — in-memory artifacts (e.g. JSON)

    Returns the list of object keys written (empty list if archiving disabled).
    Never raises for a missing local file; a storage error propagates so the
    caller can decide whether to keep the local copy."""
    if not archiving_enabled():
        return []
    s3 = _client()
    bucket = os.environ["ARCHIVE_BUCKET"]
    prefix = "raw/%s/%s" % (case_number, snapshot_ts)
    keys = []
    for name, path in (files or {}).items():
        if not path or not os.path.exists(path):
            continue
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        key = "%s/%s" % (prefix, name)
        with open(path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f.read(), ContentType=ct)
        keys.append(key)
    for name, (body, ct) in (blobs or {}).items():
        key = "%s/%s" % (prefix, name)
        if isinstance(body, str):
            body = body.encode("utf-8")
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType=ct or "application/octet-stream")
        keys.append(key)
    return keys


def selftest():
    if not archiving_enabled():
        missing = [k for k in _REQUIRED if not os.environ.get(k)]
        print("archiving DISABLED — missing env: " + ", ".join(missing))
        return False
    s3 = _client()
    bucket = os.environ["ARCHIVE_BUCKET"]
    key = "raw/_selftest/probe.txt"
    payload = b"pcpeak archive ok"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="text/plain")
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        s3.delete_object(Bucket=bucket, Key=key)
    except Exception as e:
        print("archive self-test FAILED: " + str(e))
        return False
    ok = got == payload
    print("archive self-test: " + ("OK — bucket writable & readable (%s)" % bucket
                                    if ok else "FAILED — content mismatch"))
    return ok


if __name__ == "__main__":
    selftest()
