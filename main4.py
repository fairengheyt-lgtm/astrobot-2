"""
AstroVPN Bot — полная версия
- SQLite вместо JSON файлов
- 3X-UI интеграция через py3xui (авто-создание клиентов)
- Tribute webhook: парсит payload.telegram_user_id
- Telegram Stars: XTR, provider_token=""
- Scheduler: проверка истёкших подписок каждые 10 минут
- Все edit_message_text обёрнуты в try/except
- Токен и секреты из ENV переменных
- Railway: polling + aiohttp на порту 8080 одновременно
- Реферальная программа: +7 дней за каждого оплатившего друга
"""

import asyncio
import logging
import os
import uuid
import aiosqlite
import aiohttp

from datetime import datetime, timedelta
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  КОНФИГ — всё из переменных окружения
# ══════════════════════════════════════════════════════

BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW    = os.getenv("ADMIN_IDS", "")
ADMIN_IDS        = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

# 3X-UI
XUI_HOST         = os.getenv("XUI_HOST", "http://89.127.207.207")
XUI_PORT         = int(os.getenv("XUI_PORT", "2053"))
XUI_USERNAME     = os.getenv("XUI_USERNAME", "admin")
XUI_PASSWORD     = os.getenv("XUI_PASSWORD", "")
XUI_INBOUND_ID   = int(os.getenv("XUI_INBOUND_ID", "1"))
XUI_BASE_PATH    = os.getenv("XUI_BASE_PATH", "")
XUI_TOKEN        = os.getenv("XUI_TOKEN", "")

# VPN params для VLESS ссылки
VPN_SERVER_IP    = os.getenv("VPN_SERVER_IP", "89.127.207.207")
VPN_PORT         = int(os.getenv("VPN_PORT", "443"))
VPN_PUBLIC_KEY   = os.getenv("VPN_PUBLIC_KEY", "")
VPN_SHORT_ID     = os.getenv("VPN_SHORT_ID", "")
VPN_SNI          = os.getenv("VPN_SNI", "www.apple.com")
VPN_FINGERPRINT  = os.getenv("VPN_FINGERPRINT", "chrome")
VPN_FLOW         = os.getenv("VPN_FLOW", "xtls-rprx-vision")

# Оплата
STARS_AMOUNT     = int(os.getenv("STARS_AMOUNT", "199"))
PRICE_RUB        = int(os.getenv("PRICE_RUB", "199"))
TRIBUTE_LINK     = os.getenv("TRIBUTE_LINK", "https://t.me/tribute/app?startapp=dI5p")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@astrovpn_support")

# Подписка
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))

# Реферальная программа
REFERRAL_BONUS_DAYS = 7

# БД
DB_PATH = os.getenv("DB_PATH", "astrovpn.db")

# Web
PORT = int(os.getenv("PORT", "8080"))

# ══════════════════════════════════════════════════════
#  БАЗА ДАННЫХ — SQLite (работает на Railway)
# ══════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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

        # Безопасно добавляем колонку referred_by
        async with db.execute("PRAGMA table_info(users)") as cur:
            rows = await cur.fetchall()
        existing_cols = {row[1] for row in rows}
        if "referred_by" not in existing_cols:
            await db.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
            logger.info("DB: добавлена колонка users.referred_by")

        await db.commit()


async def db_get_user(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_create_user(tg_id: int, name: str, username: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (tg_id, name, username) VALUES (?,?,?)",
            (tg_id, name, username)
        )
        await db.commit()


async def db_set_subscription(tg_id: int, vpn_uuid: str, expires: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vpn_uuid=?, subscription_expires=? WHERE tg_id=?",
            (vpn_uuid, expires.isoformat(), tg_id)
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


async def db_log_subscription(tg_id: int, start: datetime, end: datetime,
                               source: str, charge_id: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subscriptions (tg_id,start_date,end_date,source,charge_id) VALUES (?,?,?,?,?)",
            (tg_id, start.isoformat(), end.isoformat(), source, charge_id)
        )
        await db.commit()


# ── Реферальная программа ──────────────────────────────

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


async def db_extend_subscription_expires(tg_id: int, new_expires: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET subscription_expires=? WHERE tg_id=?",
            (new_expires.isoformat(), tg_id)
        )
        await db.commit()


# ══════════════════════════════════════════════════════
#  3X-UI — создание/отключение клиентов
# ══════════════════════════════════════════════════════

def build_vless_link(vpn_uuid: str) -> str:
    return (
        f"vless://{vpn_uuid}@{VPN_SERVER_IP}:{VPN_PORT}"
        f"?type=tcp&security=reality"
        f"&pbk={VPN_PUBLIC_KEY}"
        f"&sni={VPN_SNI}"
        f"&fp={VPN_FINGERPRINT}"
        f"&sid={VPN_SHORT_ID}"
        f"&flow={VPN_FLOW}"
        f"#AstroVPN"
    )


def xui_base_url() -> str:
    base = f"{XUI_HOST}:{XUI_PORT}"
    if XUI_BASE_PATH:
        base = f"{base}/{XUI_BASE_PATH}"
    return base


async def xui_login(session: aiohttp.ClientSession) -> bool:
    if XUI_TOKEN:
        logger.info("3X-UI: используется API токен")
        return True
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": f"{xui_base_url()}/",
        }
        async with session.get(f"{xui_base_url()}/", headers=headers, ssl=False) as r:
            pass
        async with session.post(
            f"{xui_base_url()}/login",
            json={"username": XUI_USERNAME, "password": XUI_PASSWORD},
            headers=headers,
            ssl=False
        ) as resp:
            text = await resp.text()
            if resp.status == 200 and '"success":true' in text:
                return True
            return False
    except Exception as e:
        logger.error(f"3X-UI: ошибка логина: {e}")
        return False


def xui_auth_headers() -> dict:
    if XUI_TOKEN:
        return {"Authorization": f"Bearer {XUI_TOKEN}"}
    return {}


async def xui_create_client(tg_id: int, name: str, expire_date: datetime) -> str | None:
    try:
        vpn_uuid = str(uuid.uuid4())
        expire_ms = int(expire_date.timestamp() * 1000)

        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session):
                return None

            url = f"{xui_base_url()}/panel/api/clients/add"
            payload = {
                "client": {
                    "email": f"tg_{tg_id}",
                    "id": vpn_uuid,
                    "subId": vpn_uuid[:8],
                    "flow": VPN_FLOW,
                    "totalGB": 0,
                    "expiryTime": expire_ms,
                    "limitIp": 1,
                    "tgId": tg_id,
                    "comment": name[:50],
                    "enable": True,
                },
                "inboundIds": [XUI_INBOUND_ID],
            }
            async with session.post(url, json=payload, ssl=False, headers=xui_auth_headers()) as resp:
                text = await resp.text()
                logger.info(f"3X-UI create: status={resp.status} body={text[:300]}")
                if resp.status == 200 and '"success":true' in text:
                    logger.info(f"3X-UI: создан клиент tg_id={tg_id} uuid={vpn_uuid}")
                    return vpn_uuid
                logger.error(f"3X-UI: ошибка создания: {text[:300]}")
                return None

    except Exception as e:
        logger.error(f"3X-UI ошибка создания клиента tg_id={tg_id}: {e}")
        return None


async def xui_disable_client(vpn_uuid: str, tg_id: int) -> bool:
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session):
                return False

            email = f"tg_{tg_id}"
            url = f"{xui_base_url()}/panel/api/clients/del/{email}"
            async with session.post(url, ssl=False, headers=xui_auth_headers()) as resp:
                text = await resp.text()
                if resp.status == 200 and '"success":true' in text:
                    logger.info(f"3X-UI: удалён клиент tg_id={tg_id}")
                    return True
                return False

    except Exception as e:
        logger.error(f"3X-UI ошибка отключения tg_id={tg_id}: {e}")
        return False


async def xui_update_client_expire(vpn_uuid: str, tg_id: int, expire_date: datetime) -> bool:
    try:
        expire_ms = int(expire_date.timestamp() * 1000)
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            if not await xui_login(session):
                return False

            email = f"tg_{tg_id}"
            url = f"{xui_base_url()}/panel/api/clients/update/{email}"
            payload = {
                "email": email,
                "id": vpn_uuid,
                "subId": vpn_uuid[:8],
                "flow": VPN_FLOW,
                "totalGB": 0,
                "expiryTime": expire_ms,
                "limitIp": 1,
                "tgId": tg_id,
                "enable": True,
            }
            async with session.post(url, json=payload, ssl=False, headers=xui_auth_headers()) as resp:
                text = await resp.text()
                if resp.status == 200 and '"success":true' in text:
                    logger.info(f"3X-UI: обновлён клиент tg_id={tg_id}")
                    return True
                return False

    except Exception as e:
        logger.error(f"3X-UI ошибка обновления tg_id={tg_id}: {e}")
        return False


# ══════════════════════════════════════════════════════
#  РЕФЕРАЛЬНЫЙ БОНУС
# ══════════════════════════════════════════════════════

async def grant_referral_bonus(bot: Bot, referrer_id: int, referred_id: int):
    referrer = await db_get_user(referrer_id)
    if not referrer:
        return

    now = datetime.utcnow()
    raw = referrer.get("subscription_expires")
    if raw:
        try:
            current = datetime.fromisoformat(raw)
        except ValueError:
            current = now
    else:
        current = now

    base = current if current > now else now
    new_expires = base + timedelta(days=REFERRAL_BONUS_DAYS)

    vpn_uuid = referrer.get("vpn_uuid")
    if vpn_uuid:
        try:
            await xui_update_client_expire(vpn_uuid, referrer_id, new_expires)
        except Exception as e:
            logger.warning(f"Реф. бонус: 3X-UI update не удался для {referrer_id}: {e}")

    await db_extend_subscription_expires(referrer_id, new_expires)

    try:
        if vpn_uuid:
            text = (
                "🎁 <b>Реферальный бонус!</b>\n\n"
                "По вашей ссылке оплатил новый пользователь.\n"
                f"Вам начислено <b>+{REFERRAL_BONUS_DAYS} дней</b> к подписке ⭐\n\n"
                f"📅 Подписка теперь до: <b>{new_expires.strftime('%d.%m.%Y')}</b>"
            )
        else:
            text = (
                "🎁 <b>Реферальный бонус!</b>\n\n"
                "По вашей ссылке оплатил новый пользователь.\n"
                f"Вам начислено <b>+{REFERRAL_BONUS_DAYS} дней</b> ⭐\n\n"
                "Бонус применится, как только вы оформите подписку."
            )
        await bot.send_message(referrer_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Реф. бонус: не удалось уведомить {referrer_id}: {e}")

    logger.info(f"Реф. бонус: tg_id={referrer_id} +{REFERRAL_BONUS_DAYS}д за tg_id={referred_id}")


# ══════════════════════════════════════════════════════
#  ВЫДАЧА ПОДПИСКИ — центральная функция
# ══════════════════════════════════════════════════════

async def issue_subscription(
    bot: Bot,
    tg_id: int,
    source: str,
    charge_id: str | None = None,
) -> bool:
    user = await db_get_user(tg_id)
    if not user:
        logger.error(f"issue_subscription: пользователь {tg_id} не найден")
        return False

    now = datetime.utcnow()
    end = now + timedelta(days=SUBSCRIPTION_DAYS)

    existing_expires_raw = user.get("subscription_expires")
    if existing_expires_raw:
        try:
            existing_expires = datetime.fromisoformat(existing_expires_raw)
            if existing_expires > now:
                end = existing_expires + timedelta(days=SUBSCRIPTION_DAYS)
        except ValueError:
            pass

    existing_uuid = user.get("vpn_uuid")
    if existing_uuid:
        vpn_uuid = existing_uuid
        ok = await xui_update_client_expire(vpn_uuid, tg_id, end)
        if not ok:
            vpn_uuid = await xui_create_client(tg_id, user.get("name", f"user_{tg_id}"), end)
            if not vpn_uuid:
                try:
                    await bot.send_message(tg_id, f"❌ Ошибка обновления ключа. Напишите {SUPPORT_USERNAME}")
                except Exception:
                    pass
                return False
    else:
        vpn_uuid = await xui_create_client(tg_id, user.get("name", f"user_{tg_id}"), end)
        if not vpn_uuid:
            try:
                await bot.send_message(tg_id, f"❌ Ошибка создания ключа. Напишите {SUPPORT_USERNAME}")
            except Exception:
                pass
            return False

    await db_set_subscription(tg_id, vpn_uuid, end)
    await db_log_subscription(tg_id, now, end, source, charge_id)

    vless = build_vless_link(vpn_uuid)
    receipt = build_receipt(source, now, end, vless)
    try:
        await bot.send_message(tg_id, receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить чек {tg_id}: {e}")

    logger.info(f"Подписка выдана: tg_id={tg_id}, source={source}, uuid={vpn_uuid}, до={end}")

    referrer_id = user.get("referred_by")
    if referrer_id and referrer_id != tg_id:
        try:
            await grant_referral_bonus(bot, int(referrer_id), tg_id)
        except Exception as e:
            logger.error(f"Реф. бонус: ошибка для referrer={referrer_id}: {e}")

    return True


def build_receipt(source: str, start: datetime, end: datetime, vless: str) -> str:
    if source == "stars":
        method = f"⭐ Telegram Stars ({STARS_AMOUNT} ⭐)"
    elif source == "admin":
        method = "👤 Выдан администратором"
    else:
        method = f"💳 Tribute / СБП ({PRICE_RUB} ₽)"

    return (
        "🧾 <b>ЧЕК ОБ ОПЛАТЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Товар: VPN доступ на {SUBSCRIPTION_DAYS} дней\n"
        f"💳 Способ: {method}\n"
        f"📅 Дата: {start.strftime('%d.%m.%Y')}\n"
        f"⏳ Истекает: {end.strftime('%d.%m.%Y')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔑 Ваш ключ:\n"
        f"<code>{vless}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Нажмите на ключ чтобы скопировать\n"
        "📖 Как подключить — /guide"
    )


# ══════════════════════════════════════════════════════
#  SCHEDULER — проверка истёкших подписок каждые 10 мин
# ══════════════════════════════════════════════════════

async def check_expired_subscriptions(bot: Bot):
    logger.debug("Scheduler: проверка истёкших подписок...")
    now = datetime.utcnow()
    users = await db_active_subs()
    for user in users:
        raw = user.get("subscription_expires")
        if not raw:
            continue
        try:
            expires = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if expires < now:
            tg_id = user["tg_id"]
            vpn_uuid = user.get("vpn_uuid")
            if vpn_uuid:
                await xui_disable_client(vpn_uuid, tg_id)
            await db_revoke(tg_id)
            logger.info(f"Scheduler: подписка истекла tg_id={tg_id}")
            try:
                await bot.send_message(
                    tg_id,
                    "⚠️ Ваша подписка <b>AstroVPN</b> истекла.\n\n"
                    "Для продления нажмите /start и выберите «Получить доступ».",
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ══════════════════════════════════════════════════════
#  TRIBUTE WEBHOOK
# ══════════════════════════════════════════════════════

async def tribute_webhook_handler(request: web.Request, bot: Bot) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        logger.warning(f"Tribute webhook: битый JSON: {e}")
        return web.Response(status=400, text="Bad JSON")

    logger.info(f"Tribute webhook получен: {data}")

    if data.get("test_event") == "test_event":
        return web.Response(status=200, text="OK")

    if data.get("name") != "new_donation":
        return web.Response(status=200, text="OK")

    payload = data.get("payload")
    if not payload or not isinstance(payload, dict):
        return web.Response(status=400, text="Missing payload")

    tg_id_raw = payload.get("telegram_user_id")
    amount = payload.get("amount", 0)

    if not tg_id_raw:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ <b>Tribute: оплата без Telegram ID!</b>\n"
                    f"Сумма: {amount/100:.0f} ₽\n"
                    f"Payload: <code>{payload}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return web.Response(status=200, text="OK")

    tg_id = int(tg_id_raw)

    if amount > 500:
        amount_rub = amount / 100
    else:
        amount_rub = amount

    MIN_AMOUNT_RUB = PRICE_RUB - 1
    if amount_rub < MIN_AMOUNT_RUB:
        try:
            await bot.send_message(
                tg_id,
                f"⚠️ Получена оплата {amount_rub:.0f} ₽, но требуется {PRICE_RUB} ₽.\n"
                f"Напишите {SUPPORT_USERNAME}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return web.Response(status=200, text="OK")

    user = await db_get_user(tg_id)
    if not user:
        try:
            chat = await bot.get_chat(tg_id)
            name = chat.full_name or f"user_{tg_id}"
            username = getattr(chat, "username", None)
        except Exception:
            name = f"user_{tg_id}"
            username = None
        await db_create_user(tg_id, name, username)

    try:
        await bot.send_message(tg_id, "✅ Оплата через Tribute получена! Создаём ваш ключ...")
    except Exception:
        pass

    tribute_payment_id = str(payload.get("id", ""))
    ok = await issue_subscription(
        bot=bot,
        tg_id=tg_id,
        source="tribute",
        charge_id=tribute_payment_id,
    )

    for admin_id in ADMIN_IDS:
        try:
            user = await db_get_user(tg_id)
            status_emoji = "✅" if ok else "❌"
            await bot.send_message(
                admin_id,
                f"{status_emoji} <b>Tribute оплата</b>\n"
                f"👤 {user.get('name','?')} (<code>{tg_id}</code>)\n"
                f"💳 {amount_rub:.0f} ₽",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return web.Response(status=200, text="OK")


# ══════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════

def kb_main(has_sub: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if has_sub:
        rows.append([
            InlineKeyboardButton(text="🔑 Мой ключ", callback_data="mykey"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ])
        rows.append([InlineKeyboardButton(text="🔄 Продлить", callback_data="buy")])
    else:
        rows.append([InlineKeyboardButton(text="🚀 Получить доступ", callback_data="buy")])
    rows.append([InlineKeyboardButton(text="📱 Как подключить", callback_data="guide")])
    rows.append([InlineKeyboardButton(text="🎁 Рефералы", callback_data="referrals")])
    rows.append([InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])


def kb_buy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⭐ Telegram Stars ({STARS_AMOUNT} ⭐)",
            callback_data="pay_stars"
        )],
        [InlineKeyboardButton(
            text=f"💳 СБП / Карта ({PRICE_RUB} ₽)",
            callback_data="pay_tribute"
        )],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


def kb_guide() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🍎 iOS", callback_data="guide_ios"),
            InlineKeyboardButton(text="🤖 Android", callback_data="guide_android"),
        ],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


def kb_tribute_wait() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="tribute_check")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


# ══════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════════════

def is_sub_active(user: dict) -> bool:
    raw = user.get("subscription_expires")
    if not raw or not user.get("vpn_uuid"):
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.utcnow()
    except ValueError:
        return False


async def safe_edit(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"safe_edit fallback failed: {e}")


def format_profile(user: dict) -> str:
    name = user.get("name") or "Пользователь"
    tg_id = user.get("tg_id")
    if is_sub_active(user):
        raw = user["subscription_expires"]
        expires = datetime.fromisoformat(raw)
        days_left = max(0, (expires - datetime.utcnow()).days)
        sub_text = (
            f"✅ <b>Активна</b>\n"
            f"⏳ Истекает: {expires.strftime('%d.%m.%Y')}\n"
            f"📆 Осталось: {days_left} дн."
        )
    else:
        sub_text = "❌ <b>Нет активной подписки</b>"
    return (
        f"👤 <b>{name}</b>\n"
        f"🆔 ID: <code>{tg_id}</code>\n\n"
        f"📡 Подписка:\n{sub_text}"
    )


# ══════════════════════════════════════════════════════
#  ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════

def create_dp() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message, command: CommandObject):
        tg_id = message.from_user.id
        name = message.from_user.full_name or "Пользователь"
        username = message.from_user.username

        existing = await db_get_user(tg_id)
        await db_create_user(tg_id, name, username)

        args = (command.args or "").strip() if command else ""
        if not existing and args.startswith("ref"):
            try:
                referrer_id = int(args[3:])
                if referrer_id != tg_id:
                    referrer = await db_get_user(referrer_id)
                    if referrer:
                        await db_set_referrer(tg_id, referrer_id)
                        logger.info(f"Реф: tg_id={tg_id} приглашён referrer={referrer_id}")
            except (ValueError, TypeError):
                pass

        user = await db_get_user(tg_id)
        has_sub = is_sub_active(user) if user else False

        text = (
            "🌐 <b>Добро пожаловать в AstroVPN!</b>\n\n"
            "Быстрый VPN на базе VLESS + Reality.\n"
            "Без логов. Без ограничений.\n\n"
            f"💎 <b>{SUBSCRIPTION_DAYS} дней</b> — {STARS_AMOUNT} ⭐ или {PRICE_RUB} ₽"
        )
        await message.answer(text, reply_markup=kb_main(has_sub))

    @dp.callback_query(F.data == "menu")
    async def cb_menu(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        has_sub = is_sub_active(user) if user else False
        text = (
            "🌐 <b>AstroVPN</b>\n\n"
            f"💎 <b>{SUBSCRIPTION_DAYS} дней</b> — {STARS_AMOUNT} ⭐ или {PRICE_RUB} ₽"
        )
        await safe_edit(call.message, text, kb_main(has_sub))
        await call.answer()

    @dp.callback_query(F.data == "profile")
    async def cb_profile(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        if not user:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        await safe_edit(call.message, format_profile(user), kb_back())
        await call.answer()

    @dp.callback_query(F.data == "mykey")
    async def cb_mykey(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        if not user or not user.get("vpn_uuid") or not is_sub_active(user):
            await safe_edit(
                call.message,
                "❌ У вас нет активного ключа.\n\nНажмите <b>Получить доступ</b>.",
                kb_back()
            )
            await call.answer()
            return
        vless = build_vless_link(user["vpn_uuid"])
        raw = user["subscription_expires"]
        expires = datetime.fromisoformat(raw)
        text = (
            "🔑 <b>Ваш VPN-ключ:</b>\n\n"
            f"<code>{vless}</code>\n\n"
            f"📅 Действует до: {expires.strftime('%d.%m.%Y')}\n\n"
            "💡 Нажмите на ключ чтобы скопировать"
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    @dp.callback_query(F.data == "buy")
    async def cb_buy(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        has_sub = is_sub_active(user) if user else False
        if has_sub:
            raw = user["subscription_expires"]
            expires = datetime.fromisoformat(raw)
            text = (
                f"🔄 Подписка активна до {expires.strftime('%d.%m.%Y')}.\n\n"
                "Хотите продлить? Выберите способ оплаты:"
            )
        else:
            text = (
                f"💰 <b>Доступ на {SUBSCRIPTION_DAYS} дней</b>\n\n"
                "Выберите способ оплаты:"
            )
        await safe_edit(call.message, text, kb_buy())
        await call.answer()

    @dp.callback_query(F.data == "pay_stars")
    async def cb_pay_stars(call: types.CallbackQuery):
        await call.answer()
        await call.message.answer_invoice(
            title=f"AstroVPN — {SUBSCRIPTION_DAYS} дней",
            description=f"VPN доступ на {SUBSCRIPTION_DAYS} дней. VLESS + Reality.",
            payload=f"vpn_{SUBSCRIPTION_DAYS}d_{call.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN {SUBSCRIPTION_DAYS} дней", amount=STARS_AMOUNT)],
        )

    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        await query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def on_successful_payment(message: types.Message):
        tg_id = message.from_user.id
        charge_id = message.successful_payment.telegram_payment_charge_id
        logger.info(f"Stars оплата: tg_id={tg_id}, charge_id={charge_id}")

        user = await db_get_user(tg_id)
        if not user:
            await db_create_user(tg_id, message.from_user.full_name or "Пользователь",
                                  message.from_user.username)

        await message.answer("✅ Оплата получена! Создаём ваш ключ...")
        ok = await issue_subscription(bot=message.bot, tg_id=tg_id,
                                      source="stars", charge_id=charge_id)
        if not ok:
            await message.answer(
                f"❌ Ошибка создания ключа. Напишите {SUPPORT_USERNAME}\n"
                f"Ваш ID: <code>{tg_id}</code>",
                parse_mode="HTML"
            )
        for admin_id in ADMIN_IDS:
            try:
                stars = message.successful_payment.total_amount
                await message.bot.send_message(
                    admin_id,
                    f"⭐ <b>Новая оплата Stars!</b>\n"
                    f"👤 {message.from_user.full_name} (<code>{tg_id}</code>)\n"
                    f"⭐ {stars} Stars",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    @dp.callback_query(F.data == "pay_tribute")
    async def cb_pay_tribute(call: types.CallbackQuery):
        tg_id = call.from_user.id
        await db_create_user(tg_id, call.from_user.full_name or "Пользователь",
                             call.from_user.username)
        text = (
            "💳 <b>Оплата через СБП / Карту (Tribute)</b>\n\n"
            f"1️⃣ Перейдите по ссылке и оплатите <b>{PRICE_RUB} ₽</b>:\n"
            f"{TRIBUTE_LINK}\n\n"
            "2️⃣ После оплаты ключ придёт автоматически (до 1 мин)\n\n"
            "⚠️ <i>Не меняйте сумму — иначе оплата не зачтётся</i>"
        )
        await safe_edit(call.message, text, kb_tribute_wait())
        await call.answer()

    @dp.callback_query(F.data == "tribute_check")
    async def cb_tribute_check(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        if user and is_sub_active(user):
            raw = user["subscription_expires"]
            expires = datetime.fromisoformat(raw)
            await safe_edit(
                call.message,
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"Подписка активна до {expires.strftime('%d.%m.%Y')}\n\n"
                "Нажмите <b>🔑 Мой ключ</b> в главном меню.",
                kb_back()
            )
        else:
            await safe_edit(
                call.message,
                "⏳ <b>Оплата ещё не подтверждена.</b>\n\n"
                "Подождите 1–2 минуты и нажмите снова.\n\n"
                f"Если проблема — напишите {SUPPORT_USERNAME}",
                kb_tribute_wait()
            )
        await call.answer()

    @dp.message(Command("guide"))
    async def cmd_guide(message: types.Message):
        await message.answer("📱 <b>Выберите платформу:</b>", reply_markup=kb_guide())

    @dp.callback_query(F.data == "guide")
    async def cb_guide(call: types.CallbackQuery):
        await safe_edit(call.message, "📱 <b>Выберите платформу:</b>", kb_guide())
        await call.answer()

    @dp.callback_query(F.data == "guide_ios")
    async def cb_guide_ios(call: types.CallbackQuery):
        text = (
            "🍎 <b>Подключение на iPhone (iOS)</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📲 <b>Шаг 1.</b> Скачайте приложение <b>Streisand</b>:\n"
            "👉 https://apps.apple.com/app/streisand/id6450534064\n\n"
            "🔑 <b>Шаг 2.</b> Вернитесь в бот → нажмите <b>🔑 Мой ключ</b> "
            "→ тапните по ключу — он скопируется автоматически 📋\n\n"
            "➕ <b>Шаг 3.</b> Откройте <b>Streisand</b> → нажмите <b>+</b> "
            "в правом верхнем углу\n\n"
            "📥 <b>Шаг 4.</b> Выберите <b>«Добавить из буфера»</b>\n\n"
            "⚡ <b>Шаг 5.</b> Нажмите большой <b>тумблер</b> — VPN включён!\n\n"
            "🎉 <b>Готово! Интернет работает без ограничений.</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Если не подключается — выключите и включите тумблер снова</i>\n\n"
            f"🆘 Нужна помощь? Пишите: {SUPPORT_USERNAME}"
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    @dp.callback_query(F.data == "guide_android")
    async def cb_guide_android(call: types.CallbackQuery):
        text = (
            "🤖 <b>Подключение на Android</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📲 <b>Шаг 1.</b> Скачайте приложение <b>Hiddify</b>:\n"
            "👉 https://play.google.com/store/apps/details?id=app.hiddify.com\n\n"
            "🔹 <i>Google Play не работает? Скачайте APK:</i>\n"
            "👉 https://hiddify.com/app/\n\n"
            "🔑 <b>Шаг 2.</b> Вернитесь в бот → нажмите <b>🔑 Мой ключ</b> "
            "→ тапните по ключу — он скопируется автоматически 📋\n\n"
            "➕ <b>Шаг 3.</b> Откройте <b>Hiddify</b> → нажмите <b>+</b>\n\n"
            "📥 <b>Шаг 4.</b> Выберите <b>«Добавить из буфера обмена»</b>\n\n"
            "⚡ <b>Шаг 5.</b> Нажмите кнопку <b>«Подключить»</b> — готово!\n\n"
            "🎉 <b>Готово! Интернет работает без ограничений.</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Также работает v2rayNG — шаги те же самые</i>\n\n"
            f"🆘 Нужна помощь? Пишите: {SUPPORT_USERNAME}"
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    @dp.callback_query(F.data == "referrals")
    async def cb_referrals(call: types.CallbackQuery):
        user_id = call.from_user.id

        try:
            me = await call.bot.get_me()
            bot_username = me.username or ""
        except Exception as e:
            logger.warning(f"cb_referrals: get_me failed: {e}")
            bot_username = ""

        ref_link = (
            f"https://t.me/{bot_username}?start=ref{user_id}"
            if bot_username else f"?start=ref{user_id}"
        )
        invited = await db_count_referrals(user_id)
        bonus_days = (await db_count_referral_payments(user_id)) * REFERRAL_BONUS_DAYS

        text = (
            "🎁 <b>Реферальная программа</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"За каждую оплату приглашённого друга вы получаете "
            f"<b>+{REFERRAL_BONUS_DAYS} дней</b> к подписке ⭐\n\n"
            "🔗 <b>Ваша реферальная ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"👥 Приглашено друзей: <b>{invited}</b>\n"
            f"📅 Получено бонусных дней: <b>{bonus_days}</b>\n\n"
            "💡 <i>Поделитесь ссылкой с друзьями — они получат VPN, "
            "а вы дополнительные дни подписки!</i>"
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    @dp.callback_query(F.data == "support")
    async def cb_support(call: types.CallbackQuery):
        text = (
            f"🆘 <b>Поддержка</b>\n\n"
            f"По всем вопросам: {SUPPORT_USERNAME}\n\n"
            "⏱ Отвечаем в течение 24 часов."
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    def admin_only_check(tg_id: int) -> bool:
        return tg_id in ADMIN_IDS

    @dp.message(Command("admin"))
    async def cmd_admin(message: types.Message):
        if not admin_only_check(message.from_user.id):
            return
        text = (
            "🛠 <b>Команды администратора:</b>\n\n"
            "/users — список пользователей\n"
            "/keys — активные ключи\n"
            "/give &lt;user_id&gt; — выдать подписку\n"
            "/revoke &lt;user_id&gt; — отозвать подписку\n"
            "/addkey &lt;user_id&gt; — alias для /give\n"
        )
        await message.answer(text, parse_mode="HTML")

    @dp.message(Command("users"))
    async def cmd_users(message: types.Message):
        if not admin_only_check(message.from_user.id):
            return
        users = await db_all_users()
        if not users:
            await message.answer("Пользователей нет.")
            return
        active = sum(1 for u in users if is_sub_active(u))
        lines = [f"👥 <b>Пользователи ({len(users)}):</b>"]
        for u in users[:50]:
            status = "✅" if is_sub_active(u) else "❌"
            lines.append(f"{status} <code>{u['tg_id']}</code> — {u.get('name','?')}")
        lines.append(f"\n📊 Активных: {active}/{len(users)}")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("keys"))
    async def cmd_keys(message: types.Message):
        if not admin_only_check(message.from_user.id):
            return
        users = await db_active_subs()
        active = [u for u in users if is_sub_active(u)]
        if not active:
            await message.answer("Нет активных ключей.")
            return
        lines = [f"🔑 <b>Активные ключи ({len(active)}):</b>"]
        for u in active[:30]:
            raw = u.get("subscription_expires", "")
            exp_str = raw[:10] if raw else "?"
            uuid_short = (u.get("vpn_uuid") or "")[:18]
            lines.append(f"<code>{u['tg_id']}</code> до {exp_str}\n  <code>{uuid_short}...</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("give"))
    @dp.message(Command("addkey"))
    async def cmd_give(message: types.Message):
        if not admin_only_check(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /give &lt;user_id&gt;", parse_mode="HTML")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("Неверный user_id")
            return
        user = await db_get_user(target_id)
        if not user:
            await message.answer(f"Пользователь {target_id} не найден в БД.")
            return
        await message.answer(f"Создаю подписку для {target_id}...")
        ok = await issue_subscription(bot=message.bot, tg_id=target_id, source="admin")
        if ok:
            await message.answer(f"✅ Подписка выдана <code>{target_id}</code>", parse_mode="HTML")
        else:
            await message.answer(f"❌ Ошибка при выдаче подписки {target_id}")

    @dp.message(Command("revoke"))
    async def cmd_revoke(message: types.Message):
        if not admin_only_check(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /revoke &lt;user_id&gt;", parse_mode="HTML")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("Неверный user_id")
            return
        user = await db_get_user(target_id)
        if not user:
            await message.answer(f"Пользователь {target_id} не найден.")
            return
        vpn_uuid = user.get("vpn_uuid")
        if vpn_uuid:
            await xui_disable_client(vpn_uuid, target_id)
        await db_revoke(target_id)
        await message.answer(f"✅ Подписка отозвана у <code>{target_id}</code>", parse_mode="HTML")
        try:
            await message.bot.send_message(
                target_id,
                "⚠️ Ваша подписка AstroVPN была отозвана администратором.\n\n"
                "Для продления напишите /start",
                parse_mode="HTML"
            )
        except Exception:
            pass

    @dp.message()
    async def any_message(message: types.Message):
        tg_id = message.from_user.id
        name = message.from_user.full_name or "Пользователь"
        username = message.from_user.username
        await db_create_user(tg_id, name, username)
        user = await db_get_user(tg_id)
        has_sub = is_sub_active(user) if user else False
        text = (
            "🌐 <b>AstroVPN</b>\n\n"
            f"💎 <b>{SUBSCRIPTION_DAYS} дней</b> — {STARS_AMOUNT} ⭐ или {PRICE_RUB} ₽"
        )
        await message.answer(text, reply_markup=kb_main(has_sub), parse_mode="HTML")

    return dp


# ══════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Проверьте переменные окружения.")

    await init_db()
    logger.info("База данных инициализирована")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = create_dp()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_expired_subscriptions,
        trigger="interval",
        minutes=10,
        args=[bot],
        id="check_subs",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler запущен (каждые 10 минут)")

    app = web.Application()

    async def webhook_route(request: web.Request) -> web.Response:
        return await tribute_webhook_handler(request, bot)

    app.router.add_post("/tribute/webhook", webhook_route)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Tribute webhook сервер запущен на порту {PORT}")
    logger.info(f"Tribute URL: http://0.0.0.0:{PORT}/tribute/webhook")

    logger.info("Запуск бота (polling)...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
