#!/usr/bin/env python3
"""
Мост подписок Marzban -> Remnawave.
Принимает старую ссылку /<path>/<token>, достаёт username из токена (с проверкой
подписи JWT_SECRET), находит shortUuid в Remnawave и проксирует подписку.
"""
import base64
import hashlib
import os
import time
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SECRET = os.environ["JWT_SECRET"]
RW_URL = os.environ["REMNAWAVE_URL"].rstrip("/")
RW_TOKEN = os.environ["REMNAWAVE_TOKEN"]
RW_API_KEY = os.getenv("REMNAWAVE_API_KEY") or os.getenv("CADDY_AUTH_API_TOKEN")
SUB_PATH = os.getenv("SUBSCRIPTION_PATH", "sub").strip("/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
# Публичная страница подписки для браузера, напр. https://link.nnnvpn.com
SUB_PAGE_URL = os.getenv("REMNAWAVE_SUB_PAGE_URL", "").rstrip("/")

VPN_UA_HINTS = (
    "v2ray", "clash", "sing-box", "mihomo", "hiddify", "stash",
    "outline", "happ", "karing", "sfa", "sfi", "sfm", "sft", "shadowsocks",
)

JWT_PREFIX = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."

app = FastAPI(title="Subscription Bridge")

# username -> (shortUuid, expires_at)
_cache: dict[str, tuple[Optional[str], float]] = {}


def username_from_token(token: str) -> Optional[str]:
    if not token or len(token) < 15:
        return None
    if token.startswith(JWT_PREFIX):
        try:
            import jwt

            payload = jwt.decode(token, SECRET, algorithms=["HS256"])
            if payload.get("access") == "subscription":
                return payload.get("sub")
        except Exception:
            return None
        return None
    u_token = token[:-10]
    u_signature = token[-10:]
    try:
        raw = u_token.encode("utf-8")
        decoded = base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        decoded_str = decoded.decode("utf-8")
    except Exception:
        return None
    resign = base64.b64encode(
        hashlib.sha256((u_token + SECRET).encode("utf-8")).digest(), altchars=b"-_"
    ).decode("utf-8")[:10]
    if u_signature != resign:
        return None
    return decoded_str.split(",")[0]


def rw_api_headers(**extra: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {RW_TOKEN}",
        "Accept": "application/json",
    }
    if RW_API_KEY:
        headers["X-Api-Key"] = RW_API_KEY
    headers.update(extra)
    return headers


def is_browser_request(request: Request) -> bool:
    """Браузер запрашивает text/html; VPN-клиенты — нет."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return False
    ua = (request.headers.get("user-agent") or "").lower()
    return not any(hint in ua for hint in VPN_UA_HINTS)


def short_uuid_for(username: str) -> Optional[str]:
    now = time.time()
    hit = _cache.get(username)
    if hit and hit[1] > now:
        return hit[0]
    try:
        r = requests.get(
            f"{RW_URL}/api/users/by-username/{username}",
            headers=rw_api_headers(),
            timeout=15,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Remnawave unreachable")
    if r.status_code == 404:
        _cache[username] = (None, now + 30)
        return None
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail="Remnawave lookup error")
    data = r.json().get("response", r.json())
    if isinstance(data, list):
        data = data[0] if data else None
    short = data.get("shortUuid") if data else None
    _cache[username] = (short, now + CACHE_TTL)
    return short


PASS_REQ_HEADERS = ("user-agent", "accept", "accept-language")
PASS_RESP_HEADERS = (
    "content-type",
    "content-disposition",
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
    "support-url",
    "profile-web-page-url",
    "announce",
    "announce-url",
)


def proxy_to_remnawave(short: str, suffix: str, request: Request) -> Response:
    url = f"{RW_URL}/api/sub/{short}{suffix}"
    fwd = {}
    for h in PASS_REQ_HEADERS:
        v = request.headers.get(h)
        if v:
            fwd[h] = v
    for h, v in request.headers.items():
        lh = h.lower()
        if lh.startswith("x-hwid") or lh.startswith("x-device") or lh in ("hwid", "x-ver-os", "x-os-version"):
            fwd[h] = v
    if RW_API_KEY:
        fwd["X-Api-Key"] = RW_API_KEY
    params = dict(request.query_params)
    try:
        r = requests.get(url, headers=fwd, params=params, timeout=20)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Remnawave unreachable")
    out_headers = {}
    for h in PASS_RESP_HEADERS:
        if h in r.headers:
            out_headers[h] = r.headers[h]
    media = r.headers.get("content-type", "text/plain")
    return Response(content=r.content, status_code=r.status_code, headers=out_headers, media_type=media)


def resolve(token: str, request: Request, suffix: str = "") -> Response:
    username = username_from_token(token)
    if not username:
        raise HTTPException(status_code=404, detail="Not Found")
    short = short_uuid_for(username)
    if not short:
        raise HTTPException(status_code=404, detail="Not Found")
    # В браузере — редирект на страницу подписки Remnawave (link.nnnvpn.com)
    if suffix == "" and is_browser_request(request) and SUB_PAGE_URL:
        return RedirectResponse(url=f"{SUB_PAGE_URL}/{short}", status_code=302)
    return proxy_to_remnawave(short, suffix, request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get(f"/{SUB_PATH}/{{token}}")
@app.get(f"/{SUB_PATH}/{{token}}/")
def sub_root(token: str, request: Request):
    return resolve(token, request, "")


@app.get(f"/{SUB_PATH}/{{token}}/info")
def sub_info(token: str, request: Request):
    return resolve(token, request, "/info")


@app.get(f"/{SUB_PATH}/{{token}}/usage")
def sub_usage(token: str, request: Request):
    return resolve(token, request, "/usage")


@app.get(f"/{SUB_PATH}/{{token}}/{{client_type}}")
def sub_client(token: str, client_type: str, request: Request):
    allowed = {"sing-box", "clash-meta", "clash", "outline", "v2ray", "v2ray-json"}
    if client_type not in allowed:
        raise HTTPException(status_code=404, detail="Not Found")
    return resolve(token, request, f"/{client_type}")
