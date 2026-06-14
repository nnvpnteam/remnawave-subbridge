#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/nnvpnteam/remnawave-subbridge.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/remnawave-subbridge}"

echo "==> Клонирование в $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Создан .env — заполни его и запусти снова:"
  echo "    nano $INSTALL_DIR/.env"
  echo "    $INSTALL_DIR/install.sh"
  exit 0
fi

echo "==> Сборка и запуск контейнера"
docker compose up -d --build

sleep 2
if curl -sf http://127.0.0.1:8080/healthz >/dev/null; then
  echo "==> OK: мост работает на http://127.0.0.1:8080"
else
  echo "==> ОШИБКА: healthz не ответил. Логи:"
  docker compose logs --tail=30 subbridge
  exit 1
fi

echo ""
echo "Дальше:"
echo "  1) nginx: proxy_pass http://127.0.0.1:8080; в location /sub/"
echo "  2) миграция: docker compose run --rm subbridge python import_users.py --dry-run"
