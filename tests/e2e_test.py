"""
End-to-end integration test for the serverless image processing pipeline.

This script exercises the full pipeline against either LocalStack or a real
AWS account.  It performs the following steps:

  1. Upload a test image to the input S3 bucket.
  2. Poll the processed S3 bucket for the resized image.
  3. Poll the DynamoDB table for the metadata entry.
  4. Optionally inspect SQS messages on the processing queue.
  5. Print a summary of pass/fail assertions.

Usage (against LocalStack):
    export AWS_ENDPOINT_URL=http://localhost:4566
    python tests/e2e_test.py

Usage (against real AWS):
    unset AWS_ENDPOINT_URL
    python tests/e2e_test.py
"""

import io
import json
import os
import sys
import time

import boto3
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")
REGION = os.environ.get("AWS_REGION", "us-east-1")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET_NAME", "input-image-bucket-dev-v1")
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET_NAME", "processed-image-bucket-dev-v1")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE_NAME", "ImageMetadataTable")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")

POLL_INTERVAL_SECONDS = 3
MAX_POLL_ATTEMPTS = 20

TEST_IMAGE_KEY = "e2e_test/sample_image.jpg"
EXPECTED_PROCESSED_KEY = f"resized_{TEST_IMAGE_KEY}"


def _client(service):
    kwargs = {"region_name": REGION}
    if ENDPOINT_URL:
        kwargs["endpoint_url"] = ENDPOINT_URL
    return boto3.client(service, **kwargs)


def _resource(service):
    kwargs = {"region_name": REGION}
    if ENDPOINT_URL:
        kwargs["endpoint_url"] = ENDPOINT_URL
    return boto3.resource(service, **kwargs)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

def generate_test_image():
    """Create a 600x400 JPEG in memory and return the byte buffer."""
    img = Image.new("RGB", (600, 400), color=(72, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf


def upload_test_image(s3):
    """Upload the test image to the input bucket."""
    image_bytes = generate_test_image()
    s3.upload_fileobj(image_bytes, INPUT_BUCKET, TEST_IMAGE_KEY)
    print(f"  Uploaded test image to s3://{INPUT_BUCKET}/{TEST_IMAGE_KEY}")


def poll_processed_image(s3):
    """Wait for the processed image to appear in the output bucket."""
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            s3.head_object(Bucket=PROCESSED_BUCKET, Key=EXPECTED_PROCESSED_KEY)
            print(f"  Found processed image at s3://{PROCESSED_BUCKET}/{EXPECTED_PROCESSED_KEY}")
            return True
        except s3.exceptions.ClientError:
            pass
        print(f"  Attempt {attempt}/{MAX_POLL_ATTEMPTS} - processed image not yet available, retrying...")
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def verify_processed_image(s3):
    """Download the processed image and verify its dimensions."""
    buf = io.BytesIO()
    s3.download_fileobj(PROCESSED_BUCKET, EXPECTED_PROCESSED_KEY, buf)
    buf.seek(0)
    with Image.open(buf) as img:
        width, height = img.size
        print(f"  Processed image dimensions: {width}x{height}")
        assert width == 200, f"Expected width 200, got {width}"
        assert height > 0, "Height must be positive"
    return True


def poll_dynamodb_entry(table):
    """Wait for the metadata row in DynamoDB."""
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        response = table.get_item(Key={"originalKey": TEST_IMAGE_KEY})
        if "Item" in response:
            item = response["Item"]
            print(f"  DynamoDB item found: {json.dumps(item, default=str, indent=2)}")
            return item
        print(f"  Attempt {attempt}/{MAX_POLL_ATTEMPTS} - DynamoDB entry not yet available, retrying...")
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def verify_dynamodb_item(item):
    """Validate the structure and content of the DynamoDB item."""
    assert item["originalKey"] == TEST_IMAGE_KEY
    assert item["processedKey"] == EXPECTED_PROCESSED_KEY
    assert item["status"] == "SUCCESS"
    assert "timestamp" in item
    assert "processingDetails" in item

    details = item["processingDetails"]
    assert "originalWidth" in details
    assert "originalHeight" in details
    assert "newWidth" in details
    assert "newHeight" in details
    print("  DynamoDB item structure validated successfully.")
    return True


def check_sqs_messages(sqs):
    """Attempt to read and inspect messages from the processing queue (non-destructive peek)."""
    if not SQS_QUEUE_URL:
        print("  SQS_QUEUE_URL not set -- skipping SQS message verification.")
        return True

    response = sqs.receive_message(
        QueueUrl=SQS_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        VisibilityTimeout=0,  # do not hide the message from the real consumer
    )
    messages = response.get("Messages", [])
    if messages:
        body = json.loads(messages[0]["Body"])
        print(f"  SQS message sample: {json.dumps(body, indent=2)}")
    else:
        print("  No SQS messages available (likely already consumed by MetadataUpdaterLambda).")
    return True


def cleanup(s3, table):
    """Remove test artefacts so the test is repeatable."""
    try:
        s3.delete_object(Bucket=INPUT_BUCKET, Key=TEST_IMAGE_KEY)
    except Exception:
        pass
    try:
        s3.delete_object(Bucket=PROCESSED_BUCKET, Key=EXPECTED_PROCESSED_KEY)
    except Exception:
        pass
    try:
        table.delete_item(Key={"originalKey": TEST_IMAGE_KEY})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    s3 = _client("s3")
    sqs = _client("sqs")
    dynamodb = _resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)

    results = {}

    print("\n=== Step 1: Upload test image ===")
    try:
        upload_test_image(s3)
        results["upload"] = "PASS"
    except Exception as exc:
        print(f"  FAILED: {exc}")
        results["upload"] = "FAIL"
        _print_summary(results)
        return 1

    print("\n=== Step 2: Poll for processed image ===")
    if poll_processed_image(s3):
        results["processed_image_exists"] = "PASS"
    else:
        print("  FAILED: processed image did not appear within the timeout.")
        results["processed_image_exists"] = "FAIL"
        _print_summary(results)
        return 1

    print("\n=== Step 3: Verify processed image dimensions ===")
    try:
        verify_processed_image(s3)
        results["image_dimensions"] = "PASS"
    except AssertionError as exc:
        print(f"  FAILED: {exc}")
        results["image_dimensions"] = "FAIL"

    print("\n=== Step 4: Poll DynamoDB for metadata entry ===")
    item = poll_dynamodb_entry(table)
    if item:
        results["dynamodb_entry_exists"] = "PASS"
    else:
        print("  FAILED: DynamoDB entry did not appear within the timeout.")
        results["dynamodb_entry_exists"] = "FAIL"
        _print_summary(results)
        return 1

    print("\n=== Step 5: Verify DynamoDB item structure ===")
    try:
        verify_dynamodb_item(item)
        results["dynamodb_item_valid"] = "PASS"
    except AssertionError as exc:
        print(f"  FAILED: {exc}")
        results["dynamodb_item_valid"] = "FAIL"

    print("\n=== Step 6: Check SQS messages (informational) ===")
    try:
        check_sqs_messages(sqs)
        results["sqs_check"] = "PASS"
    except Exception as exc:
        print(f"  FAILED: {exc}")
        results["sqs_check"] = "FAIL"

    print("\n=== Step 7: Cleanup test artefacts ===")
    cleanup(s3, table)
    print("  Cleanup complete.")

    _print_summary(results)
    return 0 if all(v == "PASS" for v in results.values()) else 1


def _print_summary(results):
    print("\n" + "=" * 50)
    print("E2E TEST SUMMARY")
    print("=" * 50)
    for check, status in results.items():
        marker = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {marker} {check}")
    total = len(results)
    passed = sum(1 for v in results.values() if v == "PASS")
    print(f"\n  {passed}/{total} checks passed.")
    print("=" * 50)


if __name__ == "__main__":
    sys.exit(main())
