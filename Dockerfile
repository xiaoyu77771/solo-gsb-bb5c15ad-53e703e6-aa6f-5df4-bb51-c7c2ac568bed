FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# LEASE_PORT 可覆盖监听端口（默认 8000）
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${LEASE_PORT:-8000}"]
