FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tokyo \
    PORT=8081 \
    HOST=0.0.0.0 \
    CONFIG_PATH=/app/data/config.json

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Persistent storage for Dokploy & Docker volumes
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080 8081

CMD ["python", "run.py"]
