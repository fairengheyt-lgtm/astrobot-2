"""
handlers/user.py — обработчики команд клиентов:
/start, /guide, callbacks для меню/профиля/ключа/гайдов/рефералов/поддержки.
"""

import logging
from datetime import datetime

from aiogram import Router, F, types
from aiogram.filters import CommandStart, Command, CommandObject

from config import (
    SUBSCRIPTION_DAYS, STARS_AMOUNT, PRICE_RUB,
    SUPPORT_USERNAME, REFERRAL_BONUS_DAYS,
)
from database import (
    db_get_user, db_create_user, db_set_referrer,
    db_get_server, db_count_referrals, db_count_referral_payments,
    db_is_banned,
)
from xui_client import build_vless_link

logger = logging.getLogger(__name__)
router = Router(name="user")


# ══════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════

def kb_main(has_sub: bool = False) -> types.InlineKeyboardMarkup:
    rows = []
    if has_sub:
        rows.append([
            types.InlineKeyboardButton(text="🔑 Мой ключ", callback_data="mykey"),
            types.InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ])
        rows.append([types.InlineKeyboardButton(text="🔄 Продлить", callback_data="buy")])
    else:
        rows.append([types.InlineKeyboardButton(text="🚀 Получить доступ", callback_data="buy")])
    rows.append([types.InlineKeyboardButton(text="📱 Как подключить", callback_data="guide")])
    rows.append([types.InlineKeyboardButton(text="🎁 Рефералы", callback_data="referrals")])
    rows.append([types.InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])


def kb_guide() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🍎 iOS", callback_data="guide_ios"),
            types.InlineKeyboardButton(text="🤖 Android", callback_data="guide_android"),
        ],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


# ══════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
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
#  /start с реф. ссылкой
# ══════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    tg_id = message.from_user.id
    name = message.from_user.full_name or "Пользователь"
    username = message.from_user.username

    # Бан-чек
    if await db_is_banned(tg_id):
        logger.info(f"Banned user {tg_id} tried /start — ignored")
        return

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
    await message.answer(text, reply_markup=kb_main(has_sub), parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  MENU
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "menu")
async def cb_menu(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    user = await db_get_user(call.from_user.id)
    has_sub = is_sub_active(user) if user else False
    text = (
        "🌐 <b>AstroVPN</b>\n\n"
        f"💎 <b>{SUBSCRIPTION_DAYS} дней</b> — {STARS_AMOUNT} ⭐ или {PRICE_RUB} ₽"
    )
    await safe_edit(call.message, text, kb_main(has_sub))
    await call.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    user = await db_get_user(call.from_user.id)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    await safe_edit(call.message, format_profile(user), kb_back())
    await call.answer()


@router.callback_query(F.data == "mykey")
async def cb_mykey(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    user = await db_get_user(call.from_user.id)
    if not user or not user.get("vpn_uuid") or not is_sub_active(user):
        await safe_edit(
            call.message,
            "❌ У вас нет активного ключа.\n\nНажмите <b>Получить доступ</b>.",
            kb_back(),
        )
        await call.answer()
        return

    server = await db_get_server(user.get("server_id") or 1)
    if not server:
        await safe_edit(
            call.message,
            f"⚠️ Ошибка: сервер не найден. Напишите {SUPPORT_USERNAME}",
            kb_back(),
        )
        await call.answer()
        return

    vless = build_vless_link(user["vpn_uuid"], server)
    expires = datetime.fromisoformat(user["subscription_expires"])
    text = (
        "🔑 <b>Ваш VPN-ключ:</b>\n\n"
        f"<code>{vless}</code>\n\n"
        f"📅 Действует до: {expires.strftime('%d.%m.%Y')}\n"
        f"🌍 Сервер: {server.get('name','—')}\n\n"
        "💡 Нажмите на ключ чтобы скопировать"
    )
    await safe_edit(call.message, text, kb_back())
    await call.answer()


# ══════════════════════════════════════════════════════
#  GUIDE
# ══════════════════════════════════════════════════════

@router.message(Command("guide"))
async def cmd_guide(message: types.Message):
    if await db_is_banned(message.from_user.id):
        return
    await message.answer("📱 <b>Выберите платформу:</b>", reply_markup=kb_guide(), parse_mode="HTML")


@router.callback_query(F.data == "guide")
async def cb_guide(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    await safe_edit(call.message, "📱 <b>Выберите платформу:</b>", kb_guide())
    await call.answer()


@router.callback_query(F.data == "guide_ios")
async def cb_guide_ios(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
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


@router.callback_query(F.data == "guide_android")
async def cb_guide_android(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
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


# ══════════════════════════════════════════════════════
#  REFERRALS
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "referrals")
async def cb_referrals(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
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


# ══════════════════════════════════════════════════════
#  SUPPORT
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "support")
async def cb_support(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    text = (
        f"🆘 <b>Поддержка</b>\n\n"
        f"По всем вопросам: {SUPPORT_USERNAME}\n\n"
        "⏱ Отвечаем в течение 24 часов."
    )
    await safe_edit(call.message, text, kb_back())
    await call.answer()


# ══════════════════════════════════════════════════════
#  FALLBACK (любое сообщение, не пойманное другими)
# ══════════════════════════════════════════════════════

@router.message()
async def any_message(message: types.Message):
    if await db_is_banned(message.from_user.id):
        return
    # Не реагируем на оплаты (это успешный платёж)
    if getattr(message, "successful_payment", None):
        return
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
