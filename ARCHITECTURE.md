# Architecture

## Overview

This project implements a serverless image processing pipeline on AWS using an
event-driven architecture. When a user uploads an image to the input S3 bucket,
a chain of asynchronous events automatically resizes the image, applies a text
watermark, stores the processed result in a second bucket, and persists metadata
to DynamoDB.

## Architecture Diagram

```
                                              +--------------------+
                                              |  DLQProcessorErrors|
                                              |  (SQS DLQ)        |
                                              +--------^-----------+
                                                       |
                                                  error path
                                                       |
+-----------------+      S3 event      +---------------+-----------+      SQS message      +-------------------+
|  Input S3       | -----------------> |  ImageProcessor           | --------------------> | ImageProcessed    |
|  Bucket         |  ObjectCreated:*   |  Lambda                   |   success message     | Queue (SQS)       |
|                 |                    |                           |                       |                   |
| input-image-    |                    | - Download image          |                       +--------+----------+
| bucket-dev-v1   |                    | - Validate file type      |                                |
+-----------------+                    | - Resize to TARGET_WIDTH  |                                | SQS trigger
                                       | - Apply watermark         |                                v
                                       | - Upload to processed     |                       +-------------------+
                                       |   bucket                  |                       | MetadataUpdater   |
                                       +--+------------------------+                       | Lambda            |
                                          |                                                |                   |
                                          | upload                                         | - Parse message   |
                                          v                                                | - Write to        |
                                       +-----------------+                                 |   DynamoDB        |
                                       | Processed S3    |                                 +--------+----------+
                                       | Bucket          |                                          |
                                       |                 |                                          | put_item
                                       | processed-image-|                                          v
                                       | bucket-dev-v1   |                                 +-------------------+
                                       +-----------------+                                 | ImageMetadata     |
                                                                                           | Table (DynamoDB)  |
                                                                                           |                   |
                                                                                           | PK: originalKey   |
                                                                                           +-------------------+

                                                                                           +--------------------+
                                                                                           | DLQProcessed       |
                                                                                           | Messages (SQS DLQ) |
                                                                                           | maxReceiveCount: 5 |
                                                                                           +--------------------+
```

## Data Flow

1. **Image Upload** -- A client uploads an image (JPEG or PNG) to the
   `input-image-bucket-dev-v1` S3 bucket via the AWS CLI, SDK, or console.

2. **S3 Event Notification** -- The bucket is configured with an event
   notification rule for `s3:ObjectCreated:*`. The rule invokes the
   `ImageProcessorLambda` function, passing the bucket name and object key.

3. **Image Processing** -- `ImageProcessorLambda` performs the following:
   - Downloads the image from the input bucket.
   - Validates the file extension (accepts `.jpg`, `.jpeg`, `.png` only).
   - Resizes the image to `TARGET_WIDTH` pixels, preserving the aspect ratio.
   - Applies a semi-transparent text watermark (`WATERMARK_TEXT`) at the
     bottom-right corner.
   - Uploads the processed image to `processed-image-bucket-dev-v1` with the
     key prefix `resized_`.

4. **Success Message** -- On successful processing, the Lambda publishes a JSON
   message to `ImageProcessedQueue` (SQS) containing the original key, processed
   key, timestamp, status, and detailed processing metrics.

5. **Error Handling** -- If processing fails at any step, the Lambda publishes a
   JSON error message to `DLQProcessorErrors` (SQS) with the original key, error
   type, error message, and timestamp. The function never raises to the caller,
   ensuring each record in a batch can be processed independently.

6. **Metadata Persistence** -- `MetadataUpdaterLambda` is triggered by messages
   arriving on `ImageProcessedQueue`. It parses each SQS record and writes the
   metadata to `ImageMetadataTable` in DynamoDB, keyed by `originalKey`.

7. **Dead-Letter Queue** -- `ImageProcessedQueue` is configured with
   `DLQProcessedMessages` as its redrive destination with a `maxReceiveCount` of
   5. If `MetadataUpdaterLambda` fails to process a message after 5 delivery
   attempts, the message is moved to the DLQ for manual inspection.

## Design Decisions

### Stateless Lambda Functions

Both Lambda functions are fully stateless. All configuration is injected via
environment variables. Temporary files are written to `/tmp` and cleaned up in a
`finally` block. Boto3 clients are initialised at module level to take advantage
of connection reuse across warm invocations.

### Idempotency

- `ImageProcessorLambda` produces deterministic output for a given input: the
  same image always yields the same resized and watermarked result. Re-processing
  an image simply overwrites the existing processed object in S3.
- `MetadataUpdaterLambda` uses DynamoDB `put_item`, which is an upsert. Writing
  the same metadata twice is a harmless no-op.

### Least-Privilege IAM

Each Lambda function has its own IAM role with narrowly scoped permissions:

| Role                        | Permissions                                                |
|-----------------------------|------------------------------------------------------------|
| ImageProcessorLambdaRole    | `s3:GetObject` on input bucket, `s3:PutObject` on processed bucket, `sqs:SendMessage` on queues, CloudWatch Logs |
| MetadataUpdaterLambdaRole   | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on processing queue, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:GetItem` on metadata table, CloudWatch Logs |

### Error Isolation

The pipeline uses two distinct dead-letter queues:

- **DLQProcessorErrors** -- receives explicit error reports from
  `ImageProcessorLambda` when image processing fails (invalid format, corrupt
  file, S3 access error).
- **DLQProcessedMessages** -- receives messages from `ImageProcessedQueue` that
  `MetadataUpdaterLambda` could not process after 5 attempts (DynamoDB outage,
  schema mismatch).

This separation allows operators to triage failures by stage.

### Partial Batch Failure Reporting

`MetadataUpdaterLambda` supports partial batch failure by returning
`batchItemFailures` in its response. If one message in a batch fails while
others succeed, only the failed message is retried, avoiding redundant
processing of already-handled messages.

### Infrastructure as Code

All AWS resources are defined in a single CloudFormation template
(`infra/main.yaml`). The template handles the circular dependency between the S3
bucket notification and the Lambda permission by using `DependsOn`. A single
`aws cloudformation deploy` command provisions the entire stack.

### LocalStack Compatibility

Both Lambda functions accept an optional `AWS_ENDPOINT_URL` environment variable.
When set, all boto3 clients and resources are configured with this custom
endpoint, enabling seamless local development and testing against LocalStack
without code changes.

## Technology Choices

| Component          | Technology             | Rationale                                                |
|--------------------|------------------------|----------------------------------------------------------|
| Compute            | AWS Lambda (Python 3.12)| Serverless, pay-per-use, automatic scaling               |
| Object Storage     | Amazon S3              | Durable, event-notification-capable, cost-effective      |
| Message Queue      | Amazon SQS             | Fully managed, at-least-once delivery, DLQ support       |
| Database           | Amazon DynamoDB        | Serverless, single-digit-millisecond latency, pay-per-request |
| Image Processing   | Pillow (PIL)           | Mature, well-documented Python imaging library           |
| Infrastructure     | AWS CloudFormation     | Native AWS IaC, no external tooling required             |
| Local Development  | LocalStack + Docker    | Simulates AWS services locally at zero cost              |
