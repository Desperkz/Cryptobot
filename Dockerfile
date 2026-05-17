FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config.yaml ./

RUN pip install --no-cache-dir -e .

RUN mkdir -p /app/data /app/logs

EXPOSE 8000

CMD ["trading-bot", "serve", "--host", "0.0.0.0", "--port", "8000"]

