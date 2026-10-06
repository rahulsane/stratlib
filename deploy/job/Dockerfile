# Daily update job. Build from the repository root: docker build -f deploy/job/Dockerfile -t stratlib-daily .
# deploy/job/build-push.ps1 does that and pushes the image to ECR.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md config.yaml ./
COPY src ./src
# Editable, like the web instance: the app finds config.yaml relative to its source tree.
RUN pip install -e . boto3
COPY deploy/job/run_daily.py ./run_daily.py

CMD ["python", "/app/run_daily.py"]
