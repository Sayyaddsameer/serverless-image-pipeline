#!/usr/bin/env bash
# setup_localstack.sh - Provisions AWS resources in LocalStack for local development

set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"
INPUT_BUCKET="input-image-bucket-dev-v1"
PROCESSED_BUCKET="processed-image-bucket-dev-v1"

echo "Waiting for LocalStack to be ready..."
until curl -sf "${ENDPOINT}/_localstack/health" > /dev/null 2>&1; do
    sleep 2
done
echo "LocalStack is ready."

# Create S3 buckets
echo "Creating S3 buckets..."
aws --endpoint-url="${ENDPOINT}" --region "${REGION}" s3 mb "s3://${INPUT_BUCKET}" 2>/dev/null || echo "Input bucket already exists."
aws --endpoint-url="${ENDPOINT}" --region "${REGION}" s3 mb "s3://${PROCESSED_BUCKET}" 2>/dev/null || echo "Processed bucket already exists."

# Create DLQ for processor errors
echo "Creating SQS queues..."
DLQ_PROCESSOR_URL=$(aws --endpoint-url="${ENDPOINT}" --region "${REGION}" sqs create-queue \
    --queue-name DLQProcessorErrors \
    --attributes '{"VisibilityTimeout":"300","MessageRetentionPeriod":"1209600"}' \
    --query 'QueueUrl' --output text)
echo "DLQProcessorErrors URL: ${DLQ_PROCESSOR_URL}"

# Get DLQ ARN for redrive policy
DLQ_PROCESSED_URL=$(aws --endpoint-url="${ENDPOINT}" --region "${REGION}" sqs create-queue \
    --queue-name DLQProcessedMessages \
    --attributes '{"VisibilityTimeout":"300","MessageRetentionPeriod":"1209600"}' \
    --query 'QueueUrl' --output text)
echo "DLQProcessedMessages URL: ${DLQ_PROCESSED_URL}"

DLQ_PROCESSED_ARN=$(aws --endpoint-url="${ENDPOINT}" --region "${REGION}" sqs get-queue-attributes \
    --queue-url "${DLQ_PROCESSED_URL}" \
    --attribute-names QueueArn \
    --query 'Attributes.QueueArn' --output text)

# Create main processing queue with DLQ
IMAGE_QUEUE_URL=$(aws --endpoint-url="${ENDPOINT}" --region "${REGION}" sqs create-queue \
    --queue-name ImageProcessedQueue \
    --attributes "{\"VisibilityTimeout\":\"300\",\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_PROCESSED_ARN}\\\",\\\"maxReceiveCount\\\":\\\"5\\\"}\"}" \
    --query 'QueueUrl' --output text)
echo "ImageProcessedQueue URL: ${IMAGE_QUEUE_URL}"

# Create DynamoDB table
echo "Creating DynamoDB table..."
aws --endpoint-url="${ENDPOINT}" --region "${REGION}" dynamodb create-table \
    --table-name ImageMetadataTable \
    --key-schema AttributeName=originalKey,KeyType=HASH \
    --attribute-definitions AttributeName=originalKey,AttributeType=S \
    --billing-mode PAY_PER_REQUEST \
    2>/dev/null || echo "DynamoDB table already exists."

echo ""
echo "=== LocalStack Setup Complete ==="
echo "Input Bucket:          s3://${INPUT_BUCKET}"
echo "Processed Bucket:      s3://${PROCESSED_BUCKET}"
echo "ImageProcessedQueue:   ${IMAGE_QUEUE_URL}"
echo "DLQProcessorErrors:    ${DLQ_PROCESSOR_URL}"
echo "DLQProcessedMessages:  ${DLQ_PROCESSED_URL}"
echo "DynamoDB Table:        ImageMetadataTable"
echo ""
echo "Export these for local testing:"
echo "  export AWS_ENDPOINT_URL=${ENDPOINT}"
echo "  export SQS_QUEUE_URL=${IMAGE_QUEUE_URL}"
echo "  export DLQ_QUEUE_URL=${DLQ_PROCESSOR_URL}"
echo "  export PROCESSED_BUCKET_NAME=${PROCESSED_BUCKET}"
echo "  export DYNAMODB_TABLE_NAME=ImageMetadataTable"
