"""
xui_client.py — server-aware 3X-UI API.

Все функции принимают объект `server` (dict из таблицы servers) и берут
все параметры (host, port, base_path, api_token, inbound_id, flow, ...) оттуда.

build_vless_link(vpn_uuid, server) использует pub_key/sni/short_id/etc из server.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ══════════════════════════════════════════════════════

def _server_base_url(server: dict) -> str:
    """Базовый URL панели 3X-UI для API-запросов (использует api_port)."""
    host = server["host"].rstrip("/")
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"
    # api_port = порт панели 3X-UI (например 33758).
    # Fallback на старое поле `port` для случаев когда миграция ещё не отработала.
    api_port = server.get("api_port") or server.get("port")
    base = f"{host}:{api_port}"
    if server.get("base_path"):
        base = f"{base}/{server['base_path'].strip('/')}"
    return base


def _server_ip(server: dict) -> str:
    """Извлекает только host без схемы — для построения VLESS ссылки."""
    raw = server["host"]
    if raw.startswith("http://") or raw.startswith("https://"):
        return urlparse(raw).hostname or raw
    return raw


def _auth_headers(server: dict) -> dict:
    token = server.get("api_token") or ""
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def build_vless_link(vpn_uuid: str, server: dict) -> str:
    """Собирает VLESS-ссылку для клиента используя vpn_port (порт VLESS-подключения)."""
    # vpn_port = публичный порт VLESS-подключения (обычно 443).
    # НЕ путать с api_port (порт панели 3X-UI).
    vpn_port = server.get("vpn_port") or 443
    return (
        f"vless://{vpn_uuid}@{_server_ip(server)}:{vpn_port}"
        f"?type=tcp&security=reality"
        f"&pbk={server.get('public_key','')}"
        f"&sni={server.get('sni','www.apple.com')}"
        f"&fp={server.get('fingerprint','firefox')}"
        f"&sid={server.get('short_id','')}"
        f"&flow={server.get('flow','xtls-rprx-vision')}"
        f"#AstroVPN-{server.get('name','VPN').replace(' ','_')}"
    )


# ══════════════════════════════════════════════════════
#  ОСНОВНЫЕ ОПЕРАЦИИ
# ══════════════════════════════════════════════════════

async def xui_login(session: aiohttp.ClientSession, server: dict) -> bool:
    """Логин в 3X-UI. Если у сервера есть api_token — логин пропускается."""
    if server.get("api_token"):
        return True
    # Без api_token и без username/password в схеме сервера — login невозможен.
    # Оставлено как заглушка для совместимости (старые установки используют ENV).
    logger.warning(
        f"3X-UI server id={server.get('id')}: нет api_token — логин может не сработать"
    )
    return False


async def xui_create_client(
    server: dict, tg_id: int, name: str, expire_date: datetime
) -> Optional[str]:
    """Создаёт клиента в 3X-UI на указанном сервере. Возвращает UUID или None."""
    try:
        vpn_uuid = str(uuid.uuid4())
        expire_ms = int(expire_date.timestamp() * 1000)

        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session, server):
                return None

            base = _server_base_url(server)
            url = f"{base}/panel/api/clients/add"
            payload = {
                "client": {
                    "email": f"tg_{tg_id}",
                    "id": vpn_uuid,
                    "subId": vpn_uuid[:8],
                    "flow": server.get("flow", "xtls-rprx-vision"),
                    "totalGB": 0,
                    "expiryTime": expire_ms,
                    "limitIp": 1,
                    "tgId": tg_id,
                    "comment": name[:50],
                    "enable": True,
                },
                "inboundIds": [int(server["inbound_id"])],
            }
            async with session.post(url, json=payload, ssl=False, headers=_auth_headers(server)) as resp:
                text = await resp.text()
                logger.info(
                    f"3X-UI[{server.get('name','?')}] create: status={resp.status} body={text[:200]}"
                )
                if resp.status == 200 and '"success":true' in text:
                    logger.info(
                        f"3X-UI[{server.get('name','?')}]: создан tg_id={tg_id} uuid={vpn_uuid}"
                    )
                    return vpn_uuid

                # Email уже занят → удалить и пересоздать
                if "email already in use" in text.lower() or "already in use" in text.lower():
                    email = f"tg_{tg_id}"
                    del_url = f"{base}/panel/api/clients/del/{email}"
                    async with session.post(del_url, ssl=False, headers=_auth_headers(server)) as del_resp:
                        del_text = await del_resp.text()
                        logger.info(f"3X-UI delete старого: {del_resp.status} {del_text[:200]}")

                    async with session.post(url, json=payload, ssl=False, headers=_auth_headers(server)) as resp2:
                        text2 = await resp2.text()
                        logger.info(f"3X-UI create retry: status={resp2.status} body={text2[:200]}")
                        if resp2.status == 200 and '"success":true' in text2:
                            return vpn_uuid

                logger.error(f"3X-UI create error: {text[:300]}")
                return None

    except Exception as e:
        logger.error(f"3X-UI ошибка создания клиента tg_id={tg_id}: {e}")
        return None


async def xui_disable_client(server: dict, vpn_uuid: str, tg_id: int) -> bool:
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session, server):
                return False

            email = f"tg_{tg_id}"
            url = f"{_server_base_url(server)}/panel/api/clients/del/{email}"
            async with session.post(url, ssl=False, headers=_auth_headers(server)) as resp:
                text = await resp.text()
                if resp.status == 200 and '"success":true' in text:
                    logger.info(f"3X-UI[{server.get('name','?')}]: удалён tg_id={tg_id}")
                    return True
                logger.error(f"3X-UI delete error: {text[:200]}")
                return False

    except Exception as e:
        logger.error(f"3X-UI ошибка удаления tg_id={tg_id}: {e}")
        return False


async def xui_update_client_expire(
    server: dict, vpn_uuid: str, tg_id: int, expire_date: datetime
) -> bool:
    try:
        expire_ms = int(expire_date.timestamp() * 1000)
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session, server):
                return False

            email = f"tg_{tg_id}"
            url = f"{_server_base_url(server)}/panel/api/clients/update/{email}"
            payload = {
                "email": email,
                "id": vpn_uuid,
                "subId": vpn_uuid[:8],
                "flow": server.get("flow", "xtls-rprx-vision"),
                "totalGB": 0,
                "expiryTime": expire_ms,
                "limitIp": 1,
                "tgId": tg_id,
                "enable": True,
            }
            async with session.post(url, json=payload, ssl=False, headers=_auth_headers(server)) as resp:
                text = await resp.text()
                if resp.status == 200 and '"success":true' in text:
                    logger.info(f"3X-UI[{server.get('name','?')}]: обновлён tg_id={tg_id}")
                    return True
                logger.error(f"3X-UI update error: {text[:200]}")
                return False

    except Exception as e:
        logger.error(f"3X-UI ошибка обновления tg_id={tg_id}: {e}")
        return False
