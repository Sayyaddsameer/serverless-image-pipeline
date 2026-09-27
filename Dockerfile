FROM public.ecr.aws/lambda/python:3.12 AS base

# Build stage for image_processor
FROM base AS image-processor
COPY src/image_processor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -t /opt/python/
COPY src/image_processor/app.py ${LAMBDA_TASK_ROOT}/app.py
CMD ["app.handler"]

# Build stage for metadata_updater
FROM base AS metadata-updater
COPY src/metadata_updater/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -t /opt/python/
COPY src/metadata_updater/app.py ${LAMBDA_TASK_ROOT}/app.py
CMD ["app.handler"]
