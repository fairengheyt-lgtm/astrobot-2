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
"""

import asyncio
import logging
import os
import uuid
import aiosqlite

from datetime import datetime, timedelta
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
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

# VPN params для VLESS ссылки
VPN_SERVER_IP    = os.getenv("VPN_SERVER_IP", "89.127.207.207")
VPN_PORT         = int(os.getenv("VPN_PORT", "443"))
VPN_PUBLIC_KEY   = os.getenv("VPN_PUBLIC_KEY", "")
VPN_SHORT_ID     = os.getenv("VPN_SHORT_ID", "")
VPN_SNI          = os.getenv("VPN_SNI", "www.apple.com")
VPN_FINGERPRINT  = os.getenv("VPN_FINGERPRINT", "chrome")
VPN_FLOW         = os.getenv("VPN_FLOW", "xtls-rprx-vision")

# Оплата
STARS_AMOUNT     = int(os.getenv("STARS_AMOUNT", "1"))
PRICE_RUB        = int(os.getenv("PRICE_RUB", "199"))
TRIBUTE_LINK     = os.getenv("TRIBUTE_LINK", "https://t.me/tribute/app?startapp=dI5p")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@astrovpn_support")

# Подписка
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))

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


# ══════════════════════════════════════════════════════
#  3X-UI — создание/отключение клиентов
# ══════════════════════════════════════════════════════

def build_vless_link(vpn_uuid: str) -> str:
    """Собирает VLESS ссылку по UUID."""
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


async def xui_create_client(tg_id: int, name: str, expire_date: datetime) -> str | None:
    """Создаёт клиента в 3X-UI. Возвращает UUID или None."""
    try:
        api = py3xui.AsyncApi(
    host=f"{XUI_HOST}:{XUI_PORT}",
    username=XUI_USERNAME,
    password=XUI_PASSWORD,
    use_tls_verify=False,
    prefix="/rIOdr1B4tPlsScark8",
)
        await api.login()

        vpn_uuid = str(uuid.uuid4())
        expire_ms = int(expire_date.timestamp() * 1000)

        client = py3xui.Client(
            id=vpn_uuid,
            email=f"tg_{tg_id}",
            enable=True,
            flow=VPN_FLOW,
            limit_ip=1,
            total_gb=0,
            expire_time=expire_ms,
            sub_id=vpn_uuid[:8],
            tg_id=str(tg_id),
            remark=name[:20],
        )
        await api.client.add(inbound_id=XUI_INBOUND_ID, clients=[client])
        logger.info(f"3X-UI: создан клиент tg_id={tg_id} uuid={vpn_uuid}")
        return vpn_uuid

    except ImportError:
        # py3xui не установлен — генерируем UUID без реального создания (dev режим)
        logger.warning("py3xui не установлен, используем UUID-заглушку")
        return str(uuid.uuid4())
    except Exception as e:
        logger.error(f"3X-UI ошибка создания клиента tg_id={tg_id}: {e}")
        return None


async def xui_disable_client(vpn_uuid: str, tg_id: int) -> bool:
    """Отключает клиента в 3X-UI."""
    try:
        import py3xui
        api = py3xui.AsyncApi(
            host=f"{XUI_HOST}:{XUI_PORT}",
            username=XUI_USERNAME,
            password=XUI_PASSWORD,
        )
        await api.login()
        client = py3xui.Client(
            id=vpn_uuid,
            email=f"tg_{tg_id}",
            enable=False,
            flow=VPN_FLOW,
            limit_ip=1,
            total_gb=0,
            expire_time=0,
            sub_id=vpn_uuid[:8],
        )
        await api.client.update(
            client_id=vpn_uuid,
            inbound_id=XUI_INBOUND_ID,
            client=client,
        )
        logger.info(f"3X-UI: отключён клиент uuid={vpn_uuid}")
        return True
    except Exception as e:
        logger.error(f"3X-UI ошибка отключения uuid={vpn_uuid}: {e}")
        return False


# ══════════════════════════════════════════════════════
#  ВЫДАЧА ПОДПИСКИ — центральная функция
# ══════════════════════════════════════════════════════

async def issue_subscription(
    bot: Bot,
    tg_id: int,
    source: str,
    charge_id: str | None = None,
) -> bool:
    """
    Создаёт VPN клиента в 3X-UI, сохраняет в БД, отправляет чек.
    source: 'stars' | 'tribute' | 'admin'
    """
    user = await db_get_user(tg_id)
    if not user:
        logger.error(f"issue_subscription: пользователь {tg_id} не найден")
        return False

    now = datetime.utcnow()
    end = now + timedelta(days=SUBSCRIPTION_DAYS)

    # Если уже есть активная подписка — продлеваем от даты истечения
    existing_expires_raw = user.get("subscription_expires")
    if existing_expires_raw:
        try:
            existing_expires = datetime.fromisoformat(existing_expires_raw)
            if existing_expires > now:
                end = existing_expires + timedelta(days=SUBSCRIPTION_DAYS)
        except ValueError:
            pass

    # Если уже есть UUID — переиспользуем (продление)
    existing_uuid = user.get("vpn_uuid")
    if existing_uuid:
        vpn_uuid = existing_uuid
        # Обновляем expire в 3X-UI
        await xui_disable_client(vpn_uuid, tg_id)  # сначала update через disable+enable
        vpn_uuid = await xui_create_client(tg_id, user.get("name", f"user_{tg_id}"), end)
        if not vpn_uuid:
            try:
                await bot.send_message(tg_id, f"❌ Ошибка создания ключа. Напишите {SUPPORT_USERNAME}")
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
    """
    Структура реального Tribute webhook:
    {
      "name": "new_donation",
      "payload": {
        "telegram_user_id": 123456789,
        "amount": 19900,   <- копейки (199 рублей = 19900)
        "currency": "RUB",
        "id": "payment_id"
      }
    }
    Тестовый запрос: {"test_event": "test_event"} — игнорируем.
    """
    try:
        data = await request.json()
    except Exception as e:
        logger.warning(f"Tribute webhook: битый JSON: {e}")
        return web.Response(status=400, text="Bad JSON")

    logger.info(f"Tribute webhook получен: {data}")

    # Игнорируем тестовые события
    if data.get("test_event") == "test_event":
        logger.info("Tribute webhook: тестовое событие, игнорируем")
        return web.Response(status=200, text="OK")

    # Проверяем имя события
    if data.get("name") != "new_donation":
        logger.info(f"Tribute webhook: событие '{data.get('name')}', игнорируем")
        return web.Response(status=200, text="OK")

    payload = data.get("payload")
    if not payload or not isinstance(payload, dict):
        logger.warning("Tribute webhook: нет payload")
        return web.Response(status=400, text="Missing payload")

    # Получаем telegram_user_id из payload (НЕ из comment!)
    tg_id_raw = payload.get("telegram_user_id")
    amount = payload.get("amount", 0)

    if not tg_id_raw:
        logger.warning(f"Tribute webhook: нет telegram_user_id: {payload}")
        # Уведомляем админа
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

    # Проверяем сумму: 19900 копеек = 199 рублей
    MIN_AMOUNT = (PRICE_RUB * 100) - 100  # небольшая погрешность
    if amount < MIN_AMOUNT:
        logger.warning(f"Tribute webhook: сумма {amount} < {MIN_AMOUNT}, tg_id={tg_id}")
        try:
            await bot.send_message(
                tg_id,
                f"⚠️ Получена оплата {amount/100:.0f} ₽, но требуется {PRICE_RUB} ₽.\n"
                f"Напишите {SUPPORT_USERNAME}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return web.Response(status=200, text="OK")

    logger.info(f"Tribute webhook: корректная оплата tg_id={tg_id}, {amount/100:.0f} ₽")

    # Создаём пользователя если не существует
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

    # Уведомляем админа
    for admin_id in ADMIN_IDS:
        try:
            user = await db_get_user(tg_id)
            status_emoji = "✅" if ok else "❌"
            await bot.send_message(
                admin_id,
                f"{status_emoji} <b>Tribute оплата</b>\n"
                f"👤 {user.get('name','?')} (<code>{tg_id}</code>)\n"
                f"💳 {amount/100:.0f} ₽",
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
    """Редактирует сообщение, игнорируя 'message not modified'."""
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
#  ХЭНДЛЕРЫ — регистрируются в create_dp()
# ══════════════════════════════════════════════════════

def create_dp() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # ── /start ──────────────────────────────────────────

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        tg_id = message.from_user.id
        name = message.from_user.full_name or "Пользователь"
        username = message.from_user.username
        await db_create_user(tg_id, name, username)
        user = await db_get_user(tg_id)
        has_sub = is_sub_active(user) if user else False

        text = (
            "🌐 <b>Добро пожаловать в AstroVPN!</b>\n\n"
            "Быстрый VPN на базе VLESS + Reality.\n"
            "Без логов. Без ограничений.\n\n"
            f"💎 <b>{SUBSCRIPTION_DAYS} дней</b> — {STARS_AMOUNT} ⭐ или {PRICE_RUB} ₽"
        )
        await message.answer(text, reply_markup=kb_main(has_sub))

    # ── menu callback ────────────────────────────────────

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

    # ── profile ──────────────────────────────────────────

    @dp.callback_query(F.data == "profile")
    async def cb_profile(call: types.CallbackQuery):
        user = await db_get_user(call.from_user.id)
        if not user:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        await safe_edit(call.message, format_profile(user), kb_back())
        await call.answer()

    # ── my key ───────────────────────────────────────────

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

    # ── buy ──────────────────────────────────────────────

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

    # ── pay stars ────────────────────────────────────────

    @dp.callback_query(F.data == "pay_stars")
    async def cb_pay_stars(call: types.CallbackQuery):
        await call.answer()
        await call.message.answer_invoice(
            title=f"AstroVPN — {SUBSCRIPTION_DAYS} дней",
            description=f"VPN доступ на {SUBSCRIPTION_DAYS} дней. VLESS + Reality.",
            payload=f"vpn_{SUBSCRIPTION_DAYS}d_{call.from_user.id}",
            provider_token="",   # обязательно пустая строка для Stars
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
        # Уведомляем админа
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

    # ── pay tribute ──────────────────────────────────────

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
        """Кнопка 'Я оплатил' — проверяем есть ли уже активная подписка."""
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

    # ── guide ────────────────────────────────────────────

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
            "🍎 <b>iOS — Amnezia VPN</b>\n\n"
            "1️⃣ Скачайте Amnezia VPN:\n"
            "https://apps.apple.com/us/app/amneziavpn/id1600529900\n\n"
            "2️⃣ Откройте бот → 🔑 Мой ключ → скопируйте ключ\n\n"
            "3️⃣ Откройте Amnezia VPN\n\n"
            "4️⃣ Нажмите <b>+</b> → <b>Вставить из буфера</b>\n\n"
            "5️⃣ Нажмите <b>Подключиться</b> ✅\n\n"
            "❓ Проблемы? Пишите: " + SUPPORT_USERNAME
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    @dp.callback_query(F.data == "guide_android")
    async def cb_guide_android(call: types.CallbackQuery):
        text = (
            "🤖 <b>Android — v2rayNG</b>\n\n"
            "1️⃣ Скачайте v2rayNG:\n"
            "https://play.google.com/store/apps/details?id=com.v2ray.ang\n\n"
            "2️⃣ Откройте бот → 🔑 Мой ключ → скопируйте ключ\n\n"
            "3️⃣ Откройте v2rayNG\n\n"
            "4️⃣ Нажмите <b>+</b> → <b>Import config from clipboard</b>\n\n"
            "5️⃣ Нажмите кнопку ▶ для подключения ✅\n\n"
            "💡 <i>Также работает Amnezia VPN — те же шаги</i>\n\n"
            "❓ Проблемы? Пишите: " + SUPPORT_USERNAME
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    # ── support ──────────────────────────────────────────

    @dp.callback_query(F.data == "support")
    async def cb_support(call: types.CallbackQuery):
        text = (
            f"🆘 <b>Поддержка</b>\n\n"
            f"По всем вопросам: {SUPPORT_USERNAME}\n\n"
            "⏱ Отвечаем в течение 24 часов."
        )
        await safe_edit(call.message, text, kb_back())
        await call.answer()

    # ── admin ────────────────────────────────────────────

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
        now = datetime.utcnow()
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

    # ── любое другое сообщение ───────────────────────────

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

    # Запускаем scheduler
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

    # Запускаем aiohttp для Tribute webhook
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
