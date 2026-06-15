# Remnawave Subbridge

Бесшовная миграция Marzban → Remnawave: мост подписок + импорт пользователей в одном Docker-контейнере.

Старая ссылка `https://your-domain:8443/sub/<token>` продолжает работать — мост декодирует токен Marzban и проксирует подписку из Remnawave.

## Что внутри

| Компонент | Назначение |
|-----------|------------|
| `bridge.py` | FastAPI-мост: токен → username → shortUuid → Remnawave |
| `import_users.py` | Импорт юзеров из MySQL Marzban в Remnawave API |
| `docker-compose.yml` | Постоянный сервис моста (`network_mode: host`) |

## Быстрая установка (1 команда)

```bash
git clone https://github.com/nnvpnteam/remnawave-subbridge.git ~/remnawave-subbridge
cd ~/remnawave-subbridge
cp .env.example .env && nano .env
docker compose up -d --build
curl -s http://127.0.0.1:8080/healthz
```

Или через скрипт:

```bash
curl -fsSL https://raw.githubusercontent.com/nnvpnteam/remnawave-subbridge/main/install.sh | bash
```

## Переменные `.env`

```bash
# Мост
JWT_SECRET=...                    # SELECT secret_key FROM jwt;
SUBSCRIPTION_PATH=sub
REMNAWAVE_URL=https://panel.example.com
REMNAWAVE_TOKEN=eyJ...

# Мигратор
MARZBAN_DATABASE_URL=mysql+pymysql://marzban:pass@127.0.0.1:3306/marzban
REMNAWAVE_SQUAD_UUID=...
TRAFFIC_LIMIT_STRATEGY=MONTH
TAG_PAID=PAID
TAG_FREE=FREE
```

### telegramId и теги

- **telegramId** — парсится из username формата `{tgid}-{4буквы}`, например `7816960148-port` → `7816960148`.
- **tag** — `PAID` если `is_trial=0`, `FREE` если `is_trial=1` (группы платников/бесплатников в Marzban).
- Имена тегов настраиваются через `TAG_PAID` / `TAG_FREE` в `.env`.

## nginx (cutover)

В `location /sub/`:

```nginx
proxy_pass http://127.0.0.1:8080;
```

Откат — вернуть `proxy_pass http://127.0.0.1:5000;` (Marzban).

## Миграция пользователей

```bash
# сухой прогон
docker compose run --rm subbridge python import_users.py --dry-run

# один юзер
docker compose run --rm subbridge python import_users.py --username testuser --verbose

# полный импорт
docker compose run --rm subbridge python import_users.py
```

## Порядок миграции

1. Заполнить `.env`, поднять контейнер
2. `--dry-run` → проверить маппинг
3. Импорт 1 юзера → сверить UUID в Remnawave
4. Полный импорт
5. nginx `/sub/` → мост
6. Проверить старую ссылку с HWID-заголовками
7. Перевести ноды, выключить Marzban

## Откат эксперимента на сервере

Если нужно вернуть всё как до эксперимента:

```bash
# 1. Остановить мост
sudo systemctl disable --now subbridge 2>/dev/null || true
pkill -f "uvicorn bridge:app" 2>/dev/null || true
docker compose -f ~/remnawave-subbridge/docker-compose.yml down 2>/dev/null || true

# 2. Вернуть nginx на Marzban
sudo sed -i 's|proxy_pass http://127.0.0.1:8080|proxy_pass http://127.0.0.1:5000|g' \
  /etc/nginx/sites-available/marzban-client
sudo nginx -t && sudo systemctl reload nginx

# 3. Удалить артефакты (опционально)
rm -rf ~/bridge ~/migration ~/remnawave-subbridge
sudo rm -f /etc/systemd/system/subbridge.service
sudo systemctl daemon-reload
```

Импортированных юзеров в Remnawave это не удалит — их можно оставить или удалить вручную в панели.

## Почему отдельный репозиторий

Это не часть форка Marzban — отдельный инструмент миграции. Его можно ставить на любой сервер с Marzban + Docker, не трогая код панели.
