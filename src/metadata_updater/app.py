import json
import logging
import os
from datetime import datetime, timezone
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_dynamodb_resource():
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint_url:
        return boto3.resource("dynamodb", endpoint_url=endpoint_url)
    return boto3.resource("dynamodb")

dynamodb = get_dynamodb_resource()
table_name = os.environ.get("DYNAMODB_TABLE_NAME")
if not table_name:
    logger.warning(json.dumps({"message": "DYNAMODB_TABLE_NAME environment variable is not set"}))
table = dynamodb.Table(table_name) if table_name else None

def handler(event, context):
    logger.info(json.dumps({
        "message": "Received event",
        "recordCount": len(event.get("Records", []))
    }))
    
    if not table:
        raise RuntimeError("DYNAMODB_TABLE_NAME environment variable is required")

    batch_item_failures = []
    
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            body = json.loads(record.get("body", "{}"))
            
            original_key = body.get("originalKey")
            if not original_key:
                raise ValueError("Missing originalKey in message body")
            
            item = {
                "originalKey": original_key,
                "processedKey": body.get("processedKey"),
                "timestamp": body.get("timestamp"),
                "status": body.get("status"),
                "processingDetails": body.get("processingDetails"),
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
            
            item = {k: v for k, v in item.items() if v is not None}
            
            table.put_item(Item=item)
            
            logger.info(json.dumps({
                "message": "Successfully processed record",
                "messageId": message_id,
                "originalKey": original_key
            }))
            
        except Exception as e:
            logger.error(json.dumps({
                "message": "Error processing record",
                "messageId": message_id,
                "error": str(e)
            }))
            batch_item_failures.append({"itemIdentifier": message_id})
            
    response = {"batchItemFailures": batch_item_failures}
    logger.info(json.dumps({
        "message": "Batch processing complete",
        "failuresCount": len(batch_item_failures)
    }))
    
    return response
