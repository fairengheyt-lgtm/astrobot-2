"""
database.py — работа с SQLite.

Содержит:
- init_db() с безопасными миграциями (через PRAGMA table_info)
- все db_* функции для users / subscriptions / servers / banned_users
- сид первого сервера из ENV при первом запуске
"""

import logging
import os
from datetime import datetime
from typing import Optional

import aiosqlite

from config import (
    DB_PATH,
    DEFAULT_SERVER_MAX_CLIENTS,
    XUI_HOST, XUI_PORT, XUI_BASE_PATH, XUI_TOKEN, XUI_INBOUND_ID,
    VPN_PORT,
    VPN_PUBLIC_KEY, VPN_SHORT_ID, VPN_SNI, VPN_FINGERPRINT, VPN_FLOW,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ + МИГРАЦИИ
# ══════════════════════════════════════════════════════

async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(row[1] == column for row in rows)


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str):
    """Эквивалент ADD COLUMN IF NOT EXISTS — SQLite такого синтаксиса не поддерживает."""
    if not await _column_exists(db, table, column):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        logger.info(f"DB: добавлена колонка {table}.{column}")


async def _ensure_renamed(db: aiosqlite.Connection, table: str, old_col: str, new_col: str):
    """Безопасно переименовывает колонку (SQLite ≥ 3.25).
    Срабатывает только если новой колонки ещё нет, а старая существует.
    """
    if await _column_exists(db, table, new_col):
        return  # уже переименовано — повторный запуск
    if not await _column_exists(db, table, old_col):
        return  # нет ни старой, ни новой — нечего делать (свежая БД)
    await db.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
    logger.info(f"DB: переименована колонка {table}.{old_col} → {new_col}")


async def init_db():
    """Создаёт таблицы (если нет) и применяет все миграции."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"DB: создана папка {db_dir}")

    async with aiosqlite.connect(DB_PATH) as db:
        # ── users ────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                name TEXT,
                username TEXT,
                vpn_uuid TEXT,
                subscription_expires TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── subscriptions ────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                plan TEXT DEFAULT '30days',
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                source TEXT NOT NULL,
                charge_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── servers (мультисервер) ───────────────────
        # api_port — порт панели 3X-UI (для API), vpn_port — порт VLESS-подключения (для ссылки клиенту).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                api_port INTEGER NOT NULL,
                vpn_port INTEGER NOT NULL,
                base_path TEXT DEFAULT '',
                api_token TEXT DEFAULT '',
                inbound_id INTEGER NOT NULL,
                public_key TEXT NOT NULL,
                short_id TEXT NOT NULL,
                sni TEXT DEFAULT 'www.apple.com',
                fingerprint TEXT DEFAULT 'firefox',
                flow TEXT DEFAULT 'xtls-rprx-vision',
                protocol TEXT DEFAULT 'vless-reality',
                max_clients INTEGER DEFAULT 35,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── banned_users ─────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                tg_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── миграции users (добавляем колонки если их нет) ──
        await _ensure_column(db, "users", "referred_by", "referred_by INTEGER")
        await _ensure_column(db, "users", "server_id", "server_id INTEGER DEFAULT 1")
        await _ensure_column(db, "users", "notified_3d_before", "notified_3d_before INTEGER DEFAULT 0")
        await _ensure_column(db, "users", "notified_1d_before", "notified_1d_before INTEGER DEFAULT 0")
        await _ensure_column(db, "users", "notified_1d_after",  "notified_1d_after INTEGER DEFAULT 0")
        await _ensure_column(db, "users", "notified_7d_after",  "notified_7d_after INTEGER DEFAULT 0")

        # ── миграции servers: разделение port на api_port + vpn_port ──
        # Старая схема имела единственное поле `port` (= порт панели 3X-UI).
        # Новая: api_port (для API) + vpn_port (для VLESS-ссылки клиенту).
        await _ensure_renamed(db, "servers", "port", "api_port")
        await _ensure_column(db, "servers", "vpn_port", "vpn_port INTEGER")
        # Бэкфилл vpn_port для существующих записей — берём из ENV (VPN_PORT, обычно 443)
        await db.execute(
            "UPDATE servers SET vpn_port=? WHERE vpn_port IS NULL",
            (VPN_PORT,)
        )

        await db.commit()

        # ── сид первого сервера из ENV ───────────────
        async with db.execute("SELECT COUNT(*) FROM servers") as cur:
            row = await cur.fetchone()
            servers_count = row[0] if row else 0

        if servers_count == 0 and XUI_HOST:
            # Берём данные из ENV переменных
            host = XUI_HOST
            if not host.startswith("http://") and not host.startswith("https://"):
                host = f"http://{host}"
            await db.execute(
                """INSERT INTO servers
                   (name, host, api_port, vpn_port, base_path, api_token, inbound_id,
                    public_key, short_id, sni, fingerprint, flow, protocol,
                    max_clients, enabled)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "Сервер 1",
                    host,
                    XUI_PORT,        # api_port — порт панели 3X-UI
                    VPN_PORT,        # vpn_port — порт VLESS-подключения
                    XUI_BASE_PATH or "",
                    XUI_TOKEN or "",
                    XUI_INBOUND_ID,
                    VPN_PUBLIC_KEY or "",
                    VPN_SHORT_ID or "",
                    VPN_SNI or "www.apple.com",
                    VPN_FINGERPRINT or "firefox",
                    VPN_FLOW or "xtls-rprx-vision",
                    "vless-reality",
                    DEFAULT_SERVER_MAX_CLIENTS,
                    1,
                )
            )
            await db.commit()
            logger.info(
                f"DB: создан первый сервер 'Сервер 1' из ENV "
                f"(api_port={XUI_PORT}, vpn_port={VPN_PORT})"
            )


# ══════════════════════════════════════════════════════
#  USERS
# ══════════════════════════════════════════════════════

async def db_get_user(tg_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_get_user_by_id(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_create_user(tg_id: int, name: str, username: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (tg_id, name, username) VALUES (?,?,?)",
            (tg_id, name, username)
        )
        await db.commit()


async def db_set_subscription(
    tg_id: int,
    vpn_uuid: str,
    expires: datetime,
    server_id: Optional[int] = None,
):
    """Устанавливает vpn_uuid + expires (+ опционально server_id) и сбрасывает флаги уведомлений."""
    async with aiosqlite.connect(DB_PATH) as db:
        if server_id is not None:
            await db.execute(
                """UPDATE users
                   SET vpn_uuid=?, subscription_expires=?, server_id=?,
                       notified_3d_before=0, notified_1d_before=0,
                       notified_1d_after=0,  notified_7d_after=0
                   WHERE tg_id=?""",
                (vpn_uuid, expires.isoformat(), server_id, tg_id)
            )
        else:
            await db.execute(
                """UPDATE users
                   SET vpn_uuid=?, subscription_expires=?,
                       notified_3d_before=0, notified_1d_before=0,
                       notified_1d_after=0,  notified_7d_after=0
                   WHERE tg_id=?""",
                (vpn_uuid, expires.isoformat(), tg_id)
            )
        await db.commit()


async def db_extend_subscription_expires(tg_id: int, new_expires: datetime):
    """Обновляет только subscription_expires + сбрасывает флаги уведомлений."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE users
               SET subscription_expires=?,
                   notified_3d_before=0, notified_1d_before=0,
                   notified_1d_after=0,  notified_7d_after=0
               WHERE tg_id=?""",
            (new_expires.isoformat(), tg_id)
        )
        await db.commit()


async def db_revoke(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vpn_uuid=NULL, subscription_expires=NULL WHERE tg_id=?",
            (tg_id,)
        )
        await db.commit()


async def db_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_active_subs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE vpn_uuid IS NOT NULL"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_count_active_subs_on_server(server_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE vpn_uuid IS NOT NULL AND server_id=?",
            (server_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_set_notified_flag(tg_id: int, flag: str):
    """flag ∈ {notified_3d_before, notified_1d_before, notified_1d_after, notified_7d_after}"""
    allowed = {"notified_3d_before", "notified_1d_before", "notified_1d_after", "notified_7d_after"}
    if flag not in allowed:
        raise ValueError(f"Unknown notification flag: {flag}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {flag}=1 WHERE tg_id=?", (tg_id,))
        await db.commit()


# ══════════════════════════════════════════════════════
#  SUBSCRIPTIONS LOG
# ══════════════════════════════════════════════════════

async def db_log_subscription(
    tg_id: int, start: datetime, end: datetime,
    source: str, charge_id: Optional[str] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subscriptions (tg_id,start_date,end_date,source,charge_id) VALUES (?,?,?,?,?)",
            (tg_id, start.isoformat(), end.isoformat(), source, charge_id)
        )
        await db.commit()


async def db_user_subscriptions(tg_id: int, limit: int = 5) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM subscriptions WHERE tg_id=? ORDER BY id DESC LIMIT ?",
            (tg_id, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_count_user_subscriptions(tg_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE tg_id=?", (tg_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_last_subscription_end(tg_id: int) -> Optional[datetime]:
    """Дата окончания самой свежей подписки пользователя (нужна для напоминаний после истечения)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT end_date FROM subscriptions WHERE tg_id=? ORDER BY id DESC LIMIT 1",
            (tg_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


async def db_revenue_since(since: datetime) -> int:
    """Сколько подписок куплено с момента since (по платным источникам)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE source IN ('stars','tribute') AND created_at >= ?",
            (since.isoformat(),)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ══════════════════════════════════════════════════════
#  REFERRALS
# ══════════════════════════════════════════════════════

async def db_set_referrer(tg_id: int, referrer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referred_by=? WHERE tg_id=? AND referred_by IS NULL",
            (referrer_id, tg_id)
        )
        await db.commit()


async def db_count_referrals(referrer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by=?", (referrer_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_count_referral_payments(referrer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions s "
            "JOIN users u ON s.tg_id = u.tg_id "
            "WHERE u.referred_by=?",
            (referrer_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_total_referrals_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ══════════════════════════════════════════════════════
#  SERVERS
# ══════════════════════════════════════════════════════

async def db_get_server(server_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM servers WHERE id=?", (server_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_all_servers(only_enabled: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM servers"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        async with db.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_create_server(data: dict) -> int:
    """Создаёт сервер. Возвращает id новой записи.

    Требует data['api_port'] (порт панели) и data['vpn_port'] (порт VLESS).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO servers
               (name, host, api_port, vpn_port, base_path, api_token, inbound_id,
                public_key, short_id, sni, fingerprint, flow, protocol,
                max_clients, enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"],
                data["host"],
                int(data["api_port"]),
                int(data["vpn_port"]),
                data.get("base_path", "") or "",
                data.get("api_token", "") or "",
                int(data["inbound_id"]),
                data["public_key"],
                data["short_id"],
                data.get("sni", "www.apple.com") or "www.apple.com",
                data.get("fingerprint", "firefox") or "firefox",
                data.get("flow", "xtls-rprx-vision") or "xtls-rprx-vision",
                data.get("protocol", "vless-reality") or "vless-reality",
                int(data.get("max_clients", DEFAULT_SERVER_MAX_CLIENTS)),
                1,
            )
        )
        await db.commit()
        return cur.lastrowid


async def db_set_server_limit(server_id: int, limit: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE servers SET max_clients=? WHERE id=?",
            (limit, server_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_set_server_enabled(server_id: int, enabled: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE servers SET enabled=? WHERE id=?",
            (1 if enabled else 0, server_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_delete_server(server_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM servers WHERE id=?", (server_id,))
        await db.commit()
        return cur.rowcount > 0


# ══════════════════════════════════════════════════════
#  BANS
# ══════════════════════════════════════════════════════

async def db_is_banned(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM banned_users WHERE tg_id=?", (tg_id,)) as cur:
            return (await cur.fetchone()) is not None


async def db_ban_user(tg_id: int, reason: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO banned_users (tg_id, reason) VALUES (?,?)",
            (tg_id, reason)
        )
        await db.commit()


async def db_unban_user(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM banned_users WHERE tg_id=?", (tg_id,))
        await db.commit()
        return cur.rowcount > 0
