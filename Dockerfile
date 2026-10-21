FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data PORT=8080 USAGE_LOG=/data/logs/usage.jsonl
RUN mkdir -p /data/logs
EXPOSE 8080
CMD ["python", "-m", "brazil_gov_contracts"]
