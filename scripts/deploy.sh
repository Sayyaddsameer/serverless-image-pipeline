#!/usr/bin/env bash
# deploy.sh - Package and deploy the serverless image processing pipeline.
#
# Usage:
#   ./scripts/deploy.sh                  # Deploy to real AWS using the default profile
#   ./scripts/deploy.sh --local          # Deploy to LocalStack
#
# Prerequisites:
#   - AWS CLI v2 installed
#   - Python 3.12 with pip
#   - zip utility

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/build"
REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="image-pipeline-stack"
S3_DEPLOY_BUCKET="${DEPLOY_BUCKET:-image-pipeline-deploy-bucket-dev-v1}"

LOCAL_MODE=false
ENDPOINT_ARGS=""

if [[ "${1:-}" == "--local" ]]; then
    LOCAL_MODE=true
    ENDPOINT_ARGS="--endpoint-url http://localhost:4566"
    echo "Deploying to LocalStack..."
else
    echo "Deploying to AWS (region: ${REGION})..."
fi

# ---------------------------------------------------------------------------
# Step 1: Package Lambda functions
# ---------------------------------------------------------------------------
echo ""
echo "=== Packaging Lambda functions ==="

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/image_processor" "${BUILD_DIR}/metadata_updater"

# ImageProcessorLambda
echo "  Packaging ImageProcessorLambda..."
cp "${PROJECT_ROOT}/src/image_processor/app.py" "${BUILD_DIR}/image_processor/"
pip install -r "${PROJECT_ROOT}/src/image_processor/requirements.txt" \
    -t "${BUILD_DIR}/image_processor/" --quiet --upgrade
(cd "${BUILD_DIR}/image_processor" && zip -r9 "${BUILD_DIR}/image_processor.zip" . -x '*.pyc' '__pycache__/*')

# MetadataUpdaterLambda
echo "  Packaging MetadataUpdaterLambda..."
cp "${PROJECT_ROOT}/src/metadata_updater/app.py" "${BUILD_DIR}/metadata_updater/"
pip install -r "${PROJECT_ROOT}/src/metadata_updater/requirements.txt" \
    -t "${BUILD_DIR}/metadata_updater/" --quiet --upgrade
(cd "${BUILD_DIR}/metadata_updater" && zip -r9 "${BUILD_DIR}/metadata_updater.zip" . -x '*.pyc' '__pycache__/*')

echo "  Packages ready:"
echo "    ${BUILD_DIR}/image_processor.zip"
echo "    ${BUILD_DIR}/metadata_updater.zip"

if [[ "${LOCAL_MODE}" == true ]]; then
    # -----------------------------------------------------------------------
    # LocalStack deployment (direct resource creation, no CloudFormation)
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Setting up LocalStack resources ==="
    bash "${SCRIPT_DIR}/setup_localstack.sh"

    echo ""
    echo "=== Creating Lambda functions in LocalStack ==="
    ENDPOINT="http://localhost:4566"

    # Fetch queue URLs
    SQS_QUEUE_URL=$(aws ${ENDPOINT_ARGS} --region "${REGION}" sqs get-queue-url \
        --queue-name ImageProcessedQueue --query 'QueueUrl' --output text)
    DLQ_QUEUE_URL=$(aws ${ENDPOINT_ARGS} --region "${REGION}" sqs get-queue-url \
        --queue-name DLQProcessorErrors --query 'QueueUrl' --output text)

    # Create or update ImageProcessorLambda
    aws ${ENDPOINT_ARGS} --region "${REGION}" lambda create-function \
        --function-name ImageProcessorLambda \
        --runtime python3.12 \
        --handler app.handler \
        --role arn:aws:iam::000000000000:role/lambda-role \
        --zip-file "fileb://${BUILD_DIR}/image_processor.zip" \
        --timeout 60 \
        --memory-size 512 \
        --environment "Variables={TARGET_WIDTH=200,WATERMARK_TEXT=(c) MyCompany,SQS_QUEUE_URL=${SQS_QUEUE_URL},DLQ_QUEUE_URL=${DLQ_QUEUE_URL},PROCESSED_BUCKET_NAME=processed-image-bucket-dev-v1,AWS_ENDPOINT_URL=${ENDPOINT}}" \
        2>/dev/null || \
    aws ${ENDPOINT_ARGS} --region "${REGION}" lambda update-function-code \
        --function-name ImageProcessorLambda \
        --zip-file "fileb://${BUILD_DIR}/image_processor.zip"

    # Create or update MetadataUpdaterLambda
    aws ${ENDPOINT_ARGS} --region "${REGION}" lambda create-function \
        --function-name MetadataUpdaterLambda \
        --runtime python3.12 \
        --handler app.handler \
        --role arn:aws:iam::000000000000:role/lambda-role \
        --zip-file "fileb://${BUILD_DIR}/metadata_updater.zip" \
        --timeout 30 \
        --memory-size 256 \
        --environment "Variables={DYNAMODB_TABLE_NAME=ImageMetadataTable,AWS_ENDPOINT_URL=${ENDPOINT}}" \
        2>/dev/null || \
    aws ${ENDPOINT_ARGS} --region "${REGION}" lambda update-function-code \
        --function-name MetadataUpdaterLambda \
        --zip-file "fileb://${BUILD_DIR}/metadata_updater.zip"

    # Set up S3 event notification
    LAMBDA_ARN=$(aws ${ENDPOINT_ARGS} --region "${REGION}" lambda get-function \
        --function-name ImageProcessorLambda --query 'Configuration.FunctionArn' --output text)

    NOTIFICATION_CONFIG=$(cat <<EOF
{
    "LambdaFunctionConfigurations": [
        {
            "LambdaFunctionArn": "${LAMBDA_ARN}",
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {"Name": "suffix", "Value": ".jpg"},
                        {"Name": "suffix", "Value": ".jpeg"},
                        {"Name": "suffix", "Value": ".png"}
                    ]
                }
            }
        }
    ]
}
EOF
)
    aws ${ENDPOINT_ARGS} --region "${REGION}" s3api put-bucket-notification-configuration \
        --bucket input-image-bucket-dev-v1 \
        --notification-configuration "${NOTIFICATION_CONFIG}" 2>/dev/null || true

    # Set up SQS event source mapping for MetadataUpdaterLambda
    SQS_ARN=$(aws ${ENDPOINT_ARGS} --region "${REGION}" sqs get-queue-attributes \
        --queue-url "${SQS_QUEUE_URL}" --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

    aws ${ENDPOINT_ARGS} --region "${REGION}" lambda create-event-source-mapping \
        --function-name MetadataUpdaterLambda \
        --event-source-arn "${SQS_ARN}" \
        --batch-size 10 \
        --enabled 2>/dev/null || true

    echo ""
    echo "=== LocalStack deployment complete ==="
    echo "Test with:"
    echo "  aws --endpoint-url=http://localhost:4566 s3 cp test_image.jpg s3://input-image-bucket-dev-v1/"

else
    # -----------------------------------------------------------------------
    # Real AWS deployment via CloudFormation
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Creating deployment S3 bucket ==="
    aws s3 mb "s3://${S3_DEPLOY_BUCKET}" --region "${REGION}" 2>/dev/null || true

    echo "  Uploading Lambda packages to s3://${S3_DEPLOY_BUCKET}..."
    aws s3 cp "${BUILD_DIR}/image_processor.zip" "s3://${S3_DEPLOY_BUCKET}/image_processor.zip"
    aws s3 cp "${BUILD_DIR}/metadata_updater.zip" "s3://${S3_DEPLOY_BUCKET}/metadata_updater.zip"

    echo ""
    echo "=== Deploying CloudFormation stack: ${STACK_NAME} ==="
    aws cloudformation deploy \
        --template-file "${PROJECT_ROOT}/infra/main.yaml" \
        --stack-name "${STACK_NAME}" \
        --capabilities CAPABILITY_IAM \
        --region "${REGION}" \
        --no-fail-on-empty-changeset

    echo ""
    echo "=== Updating Lambda function code ==="
    aws lambda update-function-code \
        --function-name ImageProcessorLambda \
        --s3-bucket "${S3_DEPLOY_BUCKET}" \
        --s3-key image_processor.zip \
        --region "${REGION}"

    aws lambda update-function-code \
        --function-name MetadataUpdaterLambda \
        --s3-bucket "${S3_DEPLOY_BUCKET}" \
        --s3-key metadata_updater.zip \
        --region "${REGION}"

    echo ""
    echo "=== Stack outputs ==="
    aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region "${REGION}" \
        --query 'Stacks[0].Outputs' \
        --output table

    echo ""
    echo "=== Deployment complete ==="
fi
