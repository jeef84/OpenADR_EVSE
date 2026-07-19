FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY services ./services
COPY scripts ./scripts
COPY config ./config

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
CMD ["tariff-engine"]
