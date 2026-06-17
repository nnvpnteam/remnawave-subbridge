#!/usr/bin/env python3
"""
Импорт пользователей из Marzban-форка (MySQL) в Remnawave через REST API.

- Читает MySQL форка ТОЛЬКО на чтение (users + proxies).
- Создаёт/обновляет юзеров в Remnawave (POST/PATCH /api/users),
  сохраняя vlessUuid / trojanPassword / ssPassword -> бесшовность по UUID.
- Идемпотентно: upsert по username (есть -> PATCH, нет -> POST).
- Поддерживает --dry-run, --limit N, --username NAME (PoC на одном юзере).
- Из username вида `7816960148-port` извлекает telegramId.
- Тег PAID/FREE по полю is_trial (платники / бесплатники).

Осознанные ограничения:
- used_traffic через API задать нельзя -> юзеры стартуют с 0 использованного.
- expireAt обязателен -> бессрочным ставится NO_EXPIRE_DATE.
- vmess не переносится (нет поля в Remnawave); переносятся vless/trojan/shadowsocks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    sys.exit("Нужен pymysql: pip install -r requirements.txt")

try:
    import requests
except ImportError:
    sys.exit("Нужен requests: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

STATUS_MAP = {
    "active": "ACTIVE",
    "disabled": "DISABLED",
    "limited": "LIMITED",
    "expired": "EXPIRED",
    "on_hold": "ACTIVE",
}

NO_EXPIRE_DATE = os.getenv("NO_EXPIRE_DATE", "2099-01-01T00:00:00.000Z")
TRAFFIC_LIMIT_STRATEGY = os.getenv("TRAFFIC_LIMIT_STRATEGY", "MONTH")
TAG_PAID = os.getenv("TAG_PAID", "PAID")
TAG_FREE = os.getenv("TAG_FREE", "FREE")

# username вида 7816960148-port (tgid + дефис + 4 буквы)
USERNAME_TG_RE = re.compile(r"^(\d+)-[a-zA-Z]{4}$")


def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = os.getenv(name, default)
    if required and not val:
        sys.exit(f"Не задана обязательная переменная окружения: {name}")
    return val


def parse_mysql_dsn() -> Dict[str, Any]:
    url = os.getenv("MARZBAN_DATABASE_URL")
    if url:
        cleaned = url.replace("mysql+pymysql://", "mysql://").replace(
            "mysql+mysqldb://", "mysql://"
        )
        p = urlparse(cleaned)
        return {
            "host": p.hostname or "127.0.0.1",
            "port": p.port or 3306,
            "user": p.username or "root",
            "password": p.password or "",
            "database": (p.path or "/marzban").lstrip("/"),
        }
    return {
        "host": env("MYSQL_HOST", "127.0.0.1"),
        "port": int(env("MYSQL_PORT", "3306")),
        "user": env("MYSQL_USER", "root"),
        "password": env("MYSQL_PASSWORD", ""),
        "database": env("MYSQL_DB", "marzban"),
    }


@dataclass
class SourceUser:
    id: int
    username: str
    status: str
    used_traffic: int
    data_limit: Optional[int]
    expire: Optional[int]
    created_at: Optional[datetime]
    note: Optional[str]
    hwid_device_limit: Optional[int]
    is_trial: bool
    vless_uuid: Optional[str] = None
    trojan_password: Optional[str] = None
    ss_password: Optional[str] = None
    flow: Optional[str] = None


def _load_settings(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def read_source_users(conn, username: Optional[str], limit: Optional[int]) -> List[SourceUser]:
    where, params = "", []
    if username:
        where = "WHERE username = %s"
        params.append(username)

    sql_users = f"""
        SELECT id, username, status, used_traffic, data_limit, expire,
               created_at, note, hwid_device_limit, is_trial
        FROM users
        {where}
        ORDER BY id
    """
    if limit:
        sql_users += f" LIMIT {int(limit)}"

    users: Dict[int, SourceUser] = {}
    with conn.cursor(DictCursor) as cur:
        cur.execute(sql_users, params)
        for r in cur.fetchall():
            users[r["id"]] = SourceUser(
                id=r["id"],
                username=r["username"],
                status=(r["status"] or "active"),
                used_traffic=int(r["used_traffic"] or 0),
                data_limit=(int(r["data_limit"]) if r["data_limit"] else None),
                expire=(int(r["expire"]) if r["expire"] else None),
                created_at=r["created_at"],
                note=r["note"],
                hwid_device_limit=(
                    int(r["hwid_device_limit"]) if r["hwid_device_limit"] is not None else None
                ),
                is_trial=bool(r["is_trial"]),
            )

    if not users:
        return []

    ids = list(users.keys())
    placeholders = ",".join(["%s"] * len(ids))
    with conn.cursor(DictCursor) as cur:
        cur.execute(
            f"SELECT user_id, type, settings FROM proxies WHERE user_id IN ({placeholders})",
            ids,
        )
        for r in cur.fetchall():
            u = users.get(r["user_id"])
            if not u:
                continue
            settings = _load_settings(r["settings"])
            ptype = (r["type"] or "").lower()
            if ptype == "vless":
                u.vless_uuid = settings.get("id")
                u.flow = settings.get("flow") or None
            elif ptype == "trojan":
                u.trojan_password = settings.get("password")
            elif ptype == "shadowsocks":
                u.ss_password = settings.get("password")

    return list(users.values())


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def expire_to_iso(expire_unix: Optional[int]) -> str:
    if not expire_unix:
        return NO_EXPIRE_DATE
    return to_iso(datetime.fromtimestamp(expire_unix, tz=timezone.utc))


def telegram_id_from_username(username: str) -> Optional[int]:
    m = USERNAME_TG_RE.match(username)
    if not m:
        return None
    return int(m.group(1))


def tag_for_user(is_trial: bool) -> str:
    return TAG_FREE if is_trial else TAG_PAID


def build_payload(u: SourceUser, squad_uuid: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "username": u.username,
        "status": STATUS_MAP.get(u.status, "ACTIVE"),
        "trafficLimitBytes": int(u.data_limit) if u.data_limit else 0,
        "trafficLimitStrategy": TRAFFIC_LIMIT_STRATEGY,
        "expireAt": expire_to_iso(u.expire),
    }
    if u.vless_uuid:
        payload["vlessUuid"] = u.vless_uuid
    if u.trojan_password:
        payload["trojanPassword"] = u.trojan_password
    if u.ss_password:
        payload["ssPassword"] = u.ss_password
    if u.note:
        payload["description"] = u.note[:500]
    if u.hwid_device_limit is not None:
        payload["hwidDeviceLimit"] = int(u.hwid_device_limit)
    if u.created_at:
        payload["createdAt"] = to_iso(u.created_at)
    if squad_uuid:
        payload["activeInternalSquads"] = [squad_uuid]

    tg_id = telegram_id_from_username(u.username)
    if tg_id is not None:
        payload["telegramId"] = tg_id
    payload["tag"] = tag_for_user(u.is_trial)

    return payload


UPDATE_FIELDS = {
    "status",
    "trafficLimitBytes",
    "trafficLimitStrategy",
    "expireAt",
    "description",
    "hwidDeviceLimit",
    "activeInternalSquads",
    "telegramId",
    "tag",
}


class Remnawave:
    def __init__(self, base_url: str, token: str, api_key: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        if api_key:
            self.s.headers["X-Api-Key"] = api_key

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        url = f"{self.base}{path}"
        last_exc = None
        for attempt in range(4):
            try:
                resp = self.s.request(method, url, timeout=30, **kw)
                if resp.status_code in (429, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return resp
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Запрос {method} {url} не удался: {last_exc}")

    def get_by_username(self, username: str) -> Optional[dict]:
        resp = self._request("GET", f"/api/users/by-username/{username}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json().get("response", resp.json())
        if isinstance(data, list):
            return data[0] if data else None
        return data

    def create(self, payload: dict) -> dict:
        resp = self._request("POST", "/api/users", data=json.dumps(payload))
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"create failed {resp.status_code}: {resp.text}")
        return resp.json().get("response", resp.json())

    def update(self, uuid: str, payload: dict) -> dict:
        body = {"uuid": uuid, **{k: v for k, v in payload.items() if k in UPDATE_FIELDS}}
        resp = self._request("PATCH", "/api/users", data=json.dumps(body))
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"update failed {resp.status_code}: {resp.text}")
        return resp.json().get("response", resp.json())


@dataclass
class Stats:
    created: int = 0
    updated: int = 0
    failed: int = 0
    mapping: List[Dict[str, str]] = field(default_factory=list)


def run(args) -> int:
    dsn = parse_mysql_dsn()
    squad_uuid = os.getenv("REMNAWAVE_SQUAD_UUID")

    print(f"[i] MySQL: {dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}")
    conn = pymysql.connect(
        host=dsn["host"],
        port=dsn["port"],
        user=dsn["user"],
        password=dsn["password"],
        database=dsn["database"],
        charset="utf8mb4",
        read_timeout=60,
    )
    try:
        users = read_source_users(conn, args.username, args.limit)
    finally:
        conn.close()

    print(f"[i] Прочитано пользователей из Marzban: {len(users)}")
    if not users:
        return 0

    if args.dry_run:
        print("[i] DRY-RUN: запросы в Remnawave не выполняются.\n")
        for u in users:
            p = build_payload(u, squad_uuid)
            creds = []
            if u.vless_uuid:
                creds.append(f"vless={u.vless_uuid}")
            if u.trojan_password:
                creds.append("trojan=***")
            if u.ss_password:
                creds.append("ss=***")
            print(
                f"  {u.username:<24} status={p['status']:<8} "
                f"tag={p.get('tag', '-'):<5} "
                f"tg={p.get('telegramId', '-'):<12} "
                f"limit={p['trafficLimitBytes']:<14} expire={p['expireAt']} "
                f"[{', '.join(creds) or 'no-proxy'}]"
            )
        print(f"\n[i] Итого к импорту: {len(users)}")
        return 0

    rw = Remnawave(
        base_url=env("REMNAWAVE_URL", required=True),
        token=env("REMNAWAVE_TOKEN", required=True),
        api_key=os.getenv("REMNAWAVE_API_KEY") or os.getenv("CADDY_AUTH_API_TOKEN"),
    )

    st = Stats()
    for i, u in enumerate(users, 1):
        payload = build_payload(u, squad_uuid)
        try:
            existing = rw.get_by_username(u.username)
            if existing:
                rw.update(existing["uuid"], payload)
                st.updated += 1
                action = "updated"
            else:
                existing = rw.create(payload)
                st.created += 1
                action = "created"

            st.mapping.append(
                {
                    "username": u.username,
                    "uuid": existing.get("uuid", ""),
                    "shortUuid": existing.get("shortUuid", ""),
                }
            )
            if i % 50 == 0 or args.verbose:
                print(f"  [{i}/{len(users)}] {action}: {u.username}")
        except Exception as e:
            st.failed += 1
            print(f"  [ERR] {u.username}: {e}", file=sys.stderr)

    print(
        f"\n[i] Готово. created={st.created} updated={st.updated} failed={st.failed} (всего {len(users)})"
    )

    if args.mapping_out and st.mapping:
        with open(args.mapping_out, "w", encoding="utf-8") as f:
            json.dump(st.mapping, f, ensure_ascii=False, indent=2)
        print(f"[i] Карта username->uuid/shortUuid: {args.mapping_out}")

    return 1 if st.failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Импорт Marzban -> Remnawave")
    ap.add_argument("--dry-run", action="store_true", help="Только показать, без записи")
    ap.add_argument("--limit", type=int, default=None, help="Импортировать только N юзеров")
    ap.add_argument("--username", type=str, default=None, help="Импортировать одного юзера (PoC)")
    ap.add_argument("--verbose", action="store_true", help="Подробный лог")
    ap.add_argument("--mapping-out", type=str, default="mapping.json", help="Карта username->uuid")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
