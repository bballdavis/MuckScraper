#!/usr/bin/env bash
# muckscraperHeadlinesGoogleNEW/restart.sh

COMPOSE_FILES=(-f docker-compose.yml)

if [ -f docker-compose.private.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.private.yml)
fi

docker compose "${COMPOSE_FILES[@]}" down
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
docker compose "${COMPOSE_FILES[@]}" up --build
