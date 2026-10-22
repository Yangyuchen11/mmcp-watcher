#!/bin/bash
# MamaChapp Bot Deployment Script for Tencent Cloud Lighthouse

set -e

echo "🚀 Deploying MamaChapp FC2 Bot..."

# Build and restart containers using docker-compose
docker compose down || true
docker compose build --no-cache
docker compose up -d

echo "✅ Container is running!"
docker compose ps
docker compose logs -f --tail=30
