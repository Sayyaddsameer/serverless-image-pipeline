import os
import json
import logging
import tempfile
import urllib.parse
import time
import uuid
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log_message(level, message, **kwargs):
    log_data = {"message": message}
    log_data.update(kwargs)
    if level == logging.INFO:
        logger.info(json.dumps(log_data))
    elif level == logging.ERROR:
        logger.error(json.dumps(log_data))
    elif level == logging.WARNING:
        logger.warning(json.dumps(log_data))

TARGET_WIDTH = int(os.environ.get("TARGET_WIDTH", "200"))
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "(c) MyCompany")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
DLQ_QUEUE_URL = os.environ.get("DLQ_QUEUE_URL")
PROCESSED_BUCKET_NAME = os.environ.get("PROCESSED_BUCKET_NAME")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")

def get_boto3_client(service_name):
    if AWS_ENDPOINT_URL:
        return boto3.client(service_name, endpoint_url=AWS_ENDPOINT_URL)
    return boto3.client(service_name)

s3_client = get_boto3_client("s3")
sqs_client = get_boto3_client("sqs")

def get_timestamp():
    return datetime.now(timezone.utc).isoformat()

def send_to_sqs(queue_url, message_body):
    try:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message_body)
        )
    except ClientError as e:
        log_message(logging.ERROR, "Failed to send message to SQS", error=str(e), queue_url=queue_url)

def send_error_to_dlq(original_key, error_type, error_message):
    message_body = {
        "originalKey": original_key,
        "errorType": error_type,
        "errorMessage": error_message,
        "timestamp": get_timestamp()
    }
    send_to_sqs(DLQ_QUEUE_URL, message_body)

def process_image(download_path, upload_path):
    with Image.open(download_path) as img:
        original_width, original_height = img.size
        original_format = img.format
        
        wpercent = (TARGET_WIDTH / float(original_width))
        hsize = int((float(original_height) * float(wpercent)))
        
        img = img.resize((TARGET_WIDTH, hsize), Image.Resampling.LANCZOS)
        
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
            
        txt_img = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_img)
        
        try:
            font = ImageFont.truetype("arial.ttf", 15)
        except IOError:
            font = ImageFont.load_default()
            
        try:
            bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
        except AttributeError:
            text_width, text_height = draw.textsize(WATERMARK_TEXT, font=font)
        
        margin = 10
        x = img.size[0] - text_width - margin
        y = img.size[1] - text_height - margin
        
        draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 128))
        
        out = Image.alpha_composite(img, txt_img)
        
        save_kwargs = {}
        
        if original_format in ('JPEG', 'JPG'):
            out = out.convert('RGB')
            save_kwargs = {'quality': 85, 'format': 'JPEG'}
        elif original_format == 'PNG':
            save_kwargs = {'format': 'PNG'}
        else:
            out = out.convert('RGB')
            save_kwargs = {'quality': 85, 'format': 'JPEG'}
            
        out.save(upload_path, **save_kwargs)
        
        return original_width, original_height, TARGET_WIDTH, hsize, save_kwargs.get('format', 'JPEG')

def handler(event, context):
    log_message(logging.INFO, "Received event", event=event)
    
    start_time = time.time()
    
    if not event.get('Records'):
        log_message(logging.WARNING, "No records found in event")
        return
        
    for record in event['Records']:
        download_path = None
        upload_path = None
        bucket_name = record['s3']['bucket']['name']
        object_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
        
        log_message(logging.INFO, "Processing record", bucket=bucket_name, key=object_key)
        
        _, ext = os.path.splitext(object_key)
        ext = ext.lower()
        if ext not in ['.jpg', '.jpeg', '.png']:
            error_msg = f"Invalid file extension: {ext}"
            log_message(logging.ERROR, error_msg, key=object_key)
            send_error_to_dlq(object_key, "ValueError", error_msg)
            return
            
        tmp_id = str(uuid.uuid4())
        tmp_dir = tempfile.gettempdir()
        download_path = os.path.join(tmp_dir, f"dl_{tmp_id}{ext}")
        upload_path = os.path.join(tmp_dir, f"ul_{tmp_id}{ext}")
        processed_key = f"resized_{object_key}"
        
        try:
            s3_client.download_file(bucket_name, object_key, download_path)
            
            orig_w, orig_h, new_w, new_h, out_fmt = process_image(download_path, upload_path)
            
            content_type = 'image/png' if out_fmt == 'PNG' else 'image/jpeg'
            s3_client.upload_file(
                upload_path, 
                PROCESSED_BUCKET_NAME, 
                processed_key,
                ExtraArgs={'ContentType': content_type}
            )
            
            file_size_bytes = os.path.getsize(upload_path)
            processing_duration_ms = int((time.time() - start_time) * 1000)
            
            success_msg = {
                "originalKey": object_key,
                "processedKey": processed_key,
                "timestamp": get_timestamp(),
                "status": "SUCCESS",
                "processingDetails": {
                    "originalWidth": orig_w,
                    "originalHeight": orig_h,
                    "newWidth": new_w,
                    "newHeight": new_h,
                    "fileSizeBytes": file_size_bytes,
                    "processingDurationMs": processing_duration_ms
                }
            }
            send_to_sqs(SQS_QUEUE_URL, success_msg)
            log_message(logging.INFO, "Successfully processed image", key=object_key, processed_key=processed_key)
            
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            log_message(logging.ERROR, "Error processing image", error=error_msg, error_type=error_type, key=object_key)
            send_error_to_dlq(object_key, error_type, error_msg)
            
        finally:
            if download_path and os.path.exists(download_path):
                os.remove(download_path)
            if upload_path and os.path.exists(upload_path):
                os.remove(upload_path)
                
    return {"statusCode": 200, "body": json.dumps("Processing complete")}
