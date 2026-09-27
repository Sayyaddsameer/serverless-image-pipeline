"""Unit tests for the MetadataUpdaterLambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

# Configure environment variables before importing the module under test
os.environ.setdefault("DYNAMODB_TABLE_NAME", "ImageMetadataTable")

# Load the module by file path to avoid collision with image_processor's app.py
_module_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
_spec = importlib.util.spec_from_file_location("metadata_updater_app", _module_path)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_sqs_event(messages):
    """Build a minimal SQS event containing one or more messages.

    Each element of *messages* can be a dict (auto-serialised to JSON) or a raw
    string (sent as-is, useful for testing malformed payloads).
    """
    records = []
    for idx, msg in enumerate(messages):
        records.append(
            {
                "messageId": f"msg-{idx}",
                "receiptHandle": f"handle-{idx}",
                "body": json.dumps(msg) if isinstance(msg, dict) else msg,
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:000000000000:ImageProcessedQueue",
                "awsRegion": "us-east-1",
            }
        )
    return {"Records": records}


def _sample_message(key="photos/test.jpg"):
    """Return a well-formed success message body as a dict."""
    return {
        "originalKey": key,
        "processedKey": f"resized_{key}",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "status": "SUCCESS",
        "processingDetails": {
            "originalWidth": 400,
            "originalHeight": 300,
            "newWidth": 200,
            "newHeight": 150,
            "fileSizeBytes": 8192,
            "processingDurationMs": 42,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandlerSingleRecordSuccess:
    """A single valid SQS message must be written to DynamoDB."""

    def test_put_item_called(self):
        event = create_sqs_event([_sample_message()])

        with patch.object(app.table, "put_item") as mock_put:
            result = app.handler(event, None)

        mock_put.assert_called_once()
        item = mock_put.call_args.kwargs["Item"]
        assert item["originalKey"] == "photos/test.jpg"
        assert item["processedKey"] == "resized_photos/test.jpg"
        assert item["status"] == "SUCCESS"
        assert "updatedAt" in item
        assert result["batchItemFailures"] == []


class TestHandlerMultipleRecords:
    """All records in a batch must be persisted independently."""

    def test_three_records(self):
        messages = [
            _sample_message("a.jpg"),
            _sample_message("b.jpg"),
            _sample_message("c.jpg"),
        ]
        event = create_sqs_event(messages)

        with patch.object(app.table, "put_item") as mock_put:
            result = app.handler(event, None)

        assert mock_put.call_count == 3
        assert result["batchItemFailures"] == []


class TestHandlerInvalidJsonBody:
    """A record with unparseable JSON must appear in batchItemFailures."""

    def test_malformed_body(self):
        event = create_sqs_event(["this is not json"])

        with patch.object(app.table, "put_item") as mock_put:
            result = app.handler(event, None)

        mock_put.assert_not_called()
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-0"


class TestHandlerDynamodbError:
    """A DynamoDB write error must surface as a batch-item failure."""

    def test_put_item_exception(self):
        event = create_sqs_event([_sample_message()])

        with patch.object(app.table, "put_item", side_effect=Exception("ConditionalCheckFailed")):
            result = app.handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-0"


class TestHandlerPartialFailure:
    """When one record out of many fails, only that record is reported."""

    def test_first_fails_second_succeeds(self):
        messages = [_sample_message("fail.jpg"), _sample_message("ok.jpg")]
        event = create_sqs_event(messages)

        call_count = {"n": 0}

        def _conditional_put(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("Simulated DynamoDB error")

        with patch.object(app.table, "put_item", side_effect=_conditional_put):
            result = app.handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-0"


class TestIdempotentWrites:
    """Processing the same message twice must simply overwrite the item."""

    def test_double_invocation(self):
        event = create_sqs_event([_sample_message()])

        with patch.object(app.table, "put_item") as mock_put:
            app.handler(event, None)
            app.handler(event, None)

        assert mock_put.call_count == 2
        # Both calls write identical data (except updatedAt)
        first_item = mock_put.call_args_list[0].kwargs["Item"]
        second_item = mock_put.call_args_list[1].kwargs["Item"]
        assert first_item["originalKey"] == second_item["originalKey"]
        assert first_item["processedKey"] == second_item["processedKey"]
