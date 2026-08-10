"""S3-compatible object storage (SeaweedFS, via the platform's ObjectStorage
attachment). Connectivity-check only for now - no read/write path yet, the
app still reads/writes DATA_DIR locally. See homelab's
docs/platform-api-usage.md for the env vars this is wired from.
"""

import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config import S3_ACCESS_KEY_ID, S3_BUCKET, S3_ENDPOINT, S3_SECRET_ACCESS_KEY

logger = logging.getLogger(__name__)


def get_s3_client():
    """A boto3 S3 client for the platform-provisioned bucket, or None if the
    app has no ObjectStorage attachment configured (e.g. running locally).
    """
    if not (S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY):
        return None
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        # SeaweedFS's S3 gateway is virtual-host-style-unaware by default;
        # path-style ("endpoint/bucket/key") is what it actually serves.
        config=Config(s3={"addressing_style": "path"}),
    )


def check_s3_connectivity() -> tuple[bool, str]:
    """HeadBucket against the configured bucket - confirms the endpoint is
    reachable and the minted credentials are actually valid and scoped to
    this bucket, not just that the env vars are present. Returns
    (ok, message); never raises, since this is a startup diagnostic, not a
    hard dependency for anything the app does today.
    """
    if not S3_BUCKET:
        return False, "S3 not configured (no ObjectStorage attachment)"

    client = get_s3_client()
    try:
        client.head_bucket(Bucket=S3_BUCKET)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        return False, f"S3 bucket {S3_BUCKET!r} unreachable: {code}"
    except BotoCoreError as e:
        return False, f"S3 endpoint {S3_ENDPOINT!r} unreachable: {e}"
    else:
        return True, f"S3 bucket {S3_BUCKET!r} reachable via {S3_ENDPOINT}"


def log_s3_connectivity() -> None:
    """Run the check once and log the result at the appropriate level -
    INFO on success, WARNING on failure. Failure is not fatal: the app has
    no S3 read/write path yet, so this is purely a startup diagnostic.
    """
    ok, message = check_s3_connectivity()
    if ok:
        logger.info(message)
    else:
        logger.warning(message)
