# Serverless Image Processing Pipeline

A resilient, scalable, event-driven image processing pipeline built on AWS
serverless services. When an image is uploaded to the input S3 bucket, it is
automatically resized, watermarked, stored in a processed bucket, and its
metadata is persisted to DynamoDB -- all without managing any servers.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup and Local Development](#setup-and-local-development)
- [Deployment to AWS](#deployment-to-aws)
- [Usage Guide](#usage-guide)
- [Testing](#testing)
- [Configuration Reference](#configuration-reference)
- [Assumptions and Trade-offs](#assumptions-and-trade-offs)
- [Future Improvements](#future-improvements)

---

## Overview

This pipeline demonstrates production-grade serverless patterns on AWS:

- **Event-driven processing** via S3 event notifications and SQS triggers.
- **Stateless Lambda functions** with environment-variable-based configuration.
- **Dead-letter queues** for both processing errors and metadata persistence
  failures.
- **Idempotent operations** that safely handle retries and duplicate invocations.
- **Least-privilege IAM roles** scoped to exactly the permissions each function
  requires.
- **Infrastructure as Code** with a single deployable CloudFormation template.
- **LocalStack compatibility** for cost-free local development and testing.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed architecture diagram,
data-flow description, and design-decision rationale.

**High-level flow:**

```
S3 Upload --> ImageProcessorLambda --> Processed S3 Bucket
                   |
                   +--> SQS (ImageProcessedQueue)
                   |          |
                   |          +--> MetadataUpdaterLambda --> DynamoDB
                   |
                   +--> SQS (DLQProcessorErrors)  [on failure]
```

## Project Structure

```
.
├── infra/
│   └── main.yaml                      # CloudFormation template (all AWS resources)
├── src/
│   ├── image_processor/               # ImageProcessorLambda
│   │   ├── __init__.py
│   │   ├── app.py                     # Lambda handler
│   │   ├── requirements.txt
│   │   └── tests/
│   │       ├── __init__.py
│   │       └── test_processor.py      # Unit tests
│   └── metadata_updater/             # MetadataUpdaterLambda
│       ├── __init__.py
│       ├── app.py                     # Lambda handler
│       ├── requirements.txt
│       └── tests/
│           ├── __init__.py
│           └── test_updater.py        # Unit tests
├── tests/
│   └── e2e_test.py                    # End-to-end integration test
├── scripts/
│   ├── setup_localstack.sh            # Provisions resources in LocalStack
│   └── deploy.sh                      # Build, package, and deploy
├── .env.example                       # Example environment variables
├── docker-compose.yml                 # LocalStack container setup
├── Dockerfile                         # Lambda packaging (multi-stage)
├── ARCHITECTURE.md                    # Architecture documentation
└── README.md                          # This file
```

## Prerequisites

- **Python 3.12** (or later) with `pip`
- **Docker** and **Docker Compose**
- **AWS CLI v2** (`aws --version`)
- **zip** utility (available on most systems)
- For real AWS deployment: a configured AWS profile with sufficient permissions

## Setup and Local Development

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2. Install Python dependencies

```bash
pip install -r src/image_processor/requirements.txt
pip install -r src/metadata_updater/requirements.txt
pip install pytest boto3 Pillow  # for running tests
```

### 3. Start LocalStack

```bash
docker-compose up -d
```

Wait for the health check to pass:

```bash
curl http://localhost:4566/_localstack/health
```

### 4. Provision resources in LocalStack

```bash
bash scripts/setup_localstack.sh
```

This creates:
- Two S3 buckets (`input-image-bucket-dev-v1`, `processed-image-bucket-dev-v1`)
- Three SQS queues (`ImageProcessedQueue`, `DLQProcessorErrors`, `DLQProcessedMessages`)
- One DynamoDB table (`ImageMetadataTable`)

### 5. Deploy Lambda functions to LocalStack

```bash
bash scripts/deploy.sh --local
```

This packages both Lambda functions with their dependencies and registers them
in LocalStack with the correct environment variables and event triggers.

### 6. Test locally

Upload a test image:

```bash
aws --endpoint-url=http://localhost:4566 s3 cp test_image.jpg s3://input-image-bucket-dev-v1/
```

Check the processed bucket:

```bash
aws --endpoint-url=http://localhost:4566 s3 ls s3://processed-image-bucket-dev-v1/
```

Check the DynamoDB table:

```bash
aws --endpoint-url=http://localhost:4566 dynamodb scan --table-name ImageMetadataTable
```

### 7. Tear down local environment

```bash
docker-compose down -v
```

## Deployment to AWS

### 1. Configure AWS credentials

```bash
aws configure
# or
export AWS_PROFILE=your-profile
```

### 2. Deploy

```bash
bash scripts/deploy.sh
```

This will:
1. Package both Lambda functions with dependencies into zip files.
2. Create a deployment S3 bucket and upload the packages.
3. Deploy the CloudFormation stack (`infra/main.yaml`).
4. Update Lambda function code to use the packaged zip files.

### 3. Verify deployment

```bash
aws cloudformation describe-stacks --stack-name image-pipeline-stack --query 'Stacks[0].Outputs' --output table
```

### 4. Tear down

```bash
aws cloudformation delete-stack --stack-name image-pipeline-stack
```

## Usage Guide

### Trigger the pipeline

Upload any JPEG or PNG image to the input bucket:

```bash
# LocalStack
aws --endpoint-url=http://localhost:4566 s3 cp photo.jpg s3://input-image-bucket-dev-v1/photos/photo.jpg

# Real AWS
aws s3 cp photo.jpg s3://input-image-bucket-dev-v1/photos/photo.jpg
```

### Check results

**Processed image:**

```bash
aws s3 ls s3://processed-image-bucket-dev-v1/
aws s3 cp s3://processed-image-bucket-dev-v1/resized_photos/photo.jpg ./resized_photo.jpg
```

**DynamoDB metadata:**

```bash
aws dynamodb get-item \
    --table-name ImageMetadataTable \
    --key '{"originalKey": {"S": "photos/photo.jpg"}}'
```

**Error queue (if any failures):**

```bash
aws sqs receive-message --queue-url <DLQProcessorErrors-URL>
```

### Expected DynamoDB item structure

```json
{
    "originalKey": "photos/photo.jpg",
    "processedKey": "resized_photos/photo.jpg",
    "timestamp": "2026-01-15T10:30:00.123456+00:00",
    "status": "SUCCESS",
    "processingDetails": {
        "originalWidth": 1920,
        "originalHeight": 1080,
        "newWidth": 200,
        "newHeight": 112,
        "fileSizeBytes": 8432,
        "processingDurationMs": 245
    },
    "updatedAt": "2026-01-15T10:30:01.456789+00:00"
}
```

## Testing

### Unit tests

```bash
# ImageProcessorLambda tests
pytest src/image_processor/tests/test_processor.py -v

# MetadataUpdaterLambda tests
pytest src/metadata_updater/tests/test_updater.py -v

# All unit tests
pytest src/ -v
```

### End-to-end integration test

Against LocalStack (ensure LocalStack is running and resources are deployed):

```bash
export AWS_ENDPOINT_URL=http://localhost:4566
python tests/e2e_test.py
```

Against real AWS:

```bash
unset AWS_ENDPOINT_URL
python tests/e2e_test.py
```

The E2E test performs the following checks:
1. Uploads a test image to the input bucket.
2. Polls the processed bucket for the resized image.
3. Verifies the processed image dimensions (width = 200 pixels).
4. Polls DynamoDB for the metadata entry.
5. Validates the metadata item structure and content.
6. Optionally inspects SQS messages.
7. Cleans up test artefacts.

## Configuration Reference

| Variable               | Lambda Function         | Default            | Description                              |
|------------------------|-------------------------|--------------------|------------------------------------------|
| `TARGET_WIDTH`         | ImageProcessorLambda    | `200`              | Target width in pixels for resizing      |
| `WATERMARK_TEXT`       | ImageProcessorLambda    | `(c) MyCompany`    | Text to overlay as a watermark           |
| `SQS_QUEUE_URL`        | ImageProcessorLambda    | (required)         | URL of the ImageProcessedQueue           |
| `DLQ_QUEUE_URL`        | ImageProcessorLambda    | (required)         | URL of the DLQProcessorErrors queue      |
| `PROCESSED_BUCKET_NAME`| ImageProcessorLambda    | (required)         | Name of the processed-image bucket       |
| `DYNAMODB_TABLE_NAME`  | MetadataUpdaterLambda   | (required)         | Name of the ImageMetadataTable           |
| `AWS_ENDPOINT_URL`     | Both                    | (unset)            | Custom endpoint for LocalStack           |

See `.env.example` for a complete list of environment variables.

## Assumptions and Trade-offs

1. **File-type validation is extension-based.** The Lambda checks the file
   extension rather than reading MIME headers. This is sufficient for the
   pipeline's use case and avoids downloading the full file before validation.

2. **Synchronous DLQ publishing.** When `ImageProcessorLambda` encounters an
   error, it explicitly sends an error message to `DLQProcessorErrors` rather
   than relying on Lambda's built-in DLQ mechanism. This gives full control over
   the error payload structure.

3. **PAY_PER_REQUEST billing for DynamoDB.** This avoids capacity planning and
   is cost-effective for variable workloads. For sustained high throughput,
   provisioned capacity with auto-scaling may be more economical.

4. **Single region deployment.** The CloudFormation template deploys to a single
   region. Multi-region replication would require S3 cross-region replication and
   DynamoDB global tables.

5. **Image format preservation.** JPEG inputs produce JPEG outputs; PNG inputs
   produce PNG outputs. No format conversion is performed.

6. **Watermark font.** The Lambda attempts to load `arial.ttf` and falls back to
   Pillow's built-in default font. The watermark appearance will vary by
   environment.

## Future Improvements

- **Image format conversion** (e.g., automatic WebP generation for web delivery).
- **Multiple output sizes** (thumbnail, medium, large) in a single invocation.
- **CloudWatch alarms** on DLQ message counts and Lambda error rates.
- **S3 lifecycle policies** to transition old processed images to cheaper storage
  classes.
- **API Gateway integration** for programmatic upload via REST endpoints.
- **Step Functions orchestration** for complex multi-step processing workflows.
- **X-Ray tracing** for distributed request tracing across Lambda invocations.
- **Lambda layers** for shared dependencies (Pillow) to reduce package size.
- **CI/CD pipeline** (GitHub Actions, CodePipeline) for automated testing and
  deployment on push.
