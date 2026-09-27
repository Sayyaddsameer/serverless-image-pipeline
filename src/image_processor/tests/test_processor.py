"""Unit tests for the ImageProcessorLambda handler."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch, ANY

import pytest
from PIL import Image

# Configure environment variables before importing the module under test
os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:4566/000000000000/ImageProcessedQueue")
os.environ.setdefault("DLQ_QUEUE_URL", "http://localhost:4566/000000000000/DLQProcessorErrors")
os.environ.setdefault("PROCESSED_BUCKET_NAME", "processed-image-bucket-dev-v1")
os.environ.setdefault("TARGET_WIDTH", "200")
os.environ.setdefault("WATERMARK_TEXT", "(c) TestCompany")

# Load the module by file path to avoid collision with metadata_updater's app.py
_module_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
_spec = importlib.util.spec_from_file_location("image_processor_app", _module_path)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_s3_event(bucket_name, object_key):
    """Build a minimal S3 ObjectCreated event payload."""
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": bucket_name},
                    "object": {"key": object_key, "size": 1024},
                },
            }
        ]
    }


def create_test_image(path, width=400, height=300, fmt="JPEG"):
    """Generate a solid-colour test image and save it to *path*."""
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    img.save(path, format=fmt)
    return path


def mock_download_side_effect(source_path, fmt="JPEG"):
    """Return a side_effect callable that copies *source_path* to the download target."""
    def _download(bucket, key, dest):
        shutil.copy(source_path, dest)
    return _download


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandlerValidJpegProcessing:
    """Verify end-to-end processing of a valid JPEG upload."""

    def test_success_flow(self, tmp_path):
        source = create_test_image(str(tmp_path / "input.jpg"), fmt="JPEG")
        event = create_s3_event("input-image-bucket-dev-v1", "photos/test.jpg")

        with patch.object(app.s3_client, "download_file", side_effect=mock_download_side_effect(source, "JPEG")), \
             patch.object(app.s3_client, "upload_file") as mock_upload, \
             patch.object(app.sqs_client, "send_message") as mock_sqs:

            result = app.handler(event, None)

        assert result["statusCode"] == 200

        # S3 upload was invoked with the correct processed key
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        assert call_args[0][1] == "processed-image-bucket-dev-v1"
        assert call_args[0][2] == "resized_photos/test.jpg"

        # SQS received a SUCCESS message on the main queue
        mock_sqs.assert_called_once()
        sqs_call = mock_sqs.call_args
        assert sqs_call.kwargs["QueueUrl"] == os.environ["SQS_QUEUE_URL"]
        body = json.loads(sqs_call.kwargs["MessageBody"])
        assert body["status"] == "SUCCESS"
        assert body["originalKey"] == "photos/test.jpg"
        assert body["processedKey"] == "resized_photos/test.jpg"
        assert "processingDetails" in body
        assert "timestamp" in body


class TestHandlerValidPngProcessing:
    """Verify end-to-end processing of a valid PNG upload."""

    def test_success_flow(self, tmp_path):
        source = create_test_image(str(tmp_path / "input.png"), fmt="PNG")
        event = create_s3_event("input-image-bucket-dev-v1", "icons/logo.png")

        with patch.object(app.s3_client, "download_file", side_effect=mock_download_side_effect(source, "PNG")), \
             patch.object(app.s3_client, "upload_file") as mock_upload, \
             patch.object(app.sqs_client, "send_message") as mock_sqs:

            result = app.handler(event, None)

        assert result["statusCode"] == 200
        mock_upload.assert_called_once()
        assert mock_upload.call_args[0][2] == "resized_icons/logo.png"

        body = json.loads(mock_sqs.call_args.kwargs["MessageBody"])
        assert body["status"] == "SUCCESS"
        assert body["originalKey"] == "icons/logo.png"


class TestHandlerInvalidFileType:
    """An unsupported file extension must route to the DLQ."""

    def test_gif_rejected(self, tmp_path):
        event = create_s3_event("input-image-bucket-dev-v1", "animations/anim.gif")

        with patch.object(app.s3_client, "download_file") as mock_dl, \
             patch.object(app.sqs_client, "send_message") as mock_sqs:

            app.handler(event, None)

        # No download should have been attempted
        mock_dl.assert_not_called()

        # Error sent to the DLQ
        mock_sqs.assert_called_once()
        sqs_call = mock_sqs.call_args
        assert sqs_call.kwargs["QueueUrl"] == os.environ["DLQ_QUEUE_URL"]
        body = json.loads(sqs_call.kwargs["MessageBody"])
        assert body["errorType"] == "ValueError"
        assert ".gif" in body["errorMessage"].lower()


class TestHandlerS3DownloadError:
    """A download failure must route to the DLQ without crashing."""

    def test_client_error(self, tmp_path):
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}}
        event = create_s3_event("input-image-bucket-dev-v1", "missing/image.jpg")

        with patch.object(app.s3_client, "download_file", side_effect=ClientError(error_response, "GetObject")), \
             patch.object(app.sqs_client, "send_message") as mock_sqs:

            result = app.handler(event, None)

        assert result["statusCode"] == 200

        mock_sqs.assert_called_once()
        body = json.loads(mock_sqs.call_args.kwargs["MessageBody"])
        assert body["errorType"] == "ClientError"
        assert body["originalKey"] == "missing/image.jpg"


class TestImageResizeMaintainsAspectRatio:
    """Resizing to TARGET_WIDTH=200 must preserve the original aspect ratio."""

    def test_aspect_ratio_preserved(self, tmp_path):
        source = create_test_image(str(tmp_path / "wide.jpg"), width=400, height=200, fmt="JPEG")
        output = str(tmp_path / "resized.jpg")

        app.process_image(source, output)
        with Image.open(output) as img:
            assert img.size[0] == 200
            assert img.size[1] == 100


class TestWatermarkApplied:
    """The watermark step must produce a valid image file."""

    def test_output_is_valid_image(self, tmp_path):
        source = create_test_image(str(tmp_path / "photo.jpg"), width=600, height=400, fmt="JPEG")
        output = str(tmp_path / "watermarked.jpg")

        app.process_image(source, output)

        assert os.path.isfile(output)
        with Image.open(output) as img:
            assert img.size[0] == 200  # TARGET_WIDTH
            assert img.size[1] > 0
