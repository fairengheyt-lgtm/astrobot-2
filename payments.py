"""
handlers/payments.py — оплата через Telegram Stars + Tribute webhook + кнопка покупки.
"""

import logging
from datetime import datetime

from aiogram import Router, F, types
from aiogram.types import LabeledPrice, PreCheckoutQuery
from aiohttp import web

from config import (
    SUBSCRIPTION_DAYS, STARS_AMOUNT, PRICE_RUB,
    SUPPORT_USERNAME, TRIBUTE_LINK, ADMIN_IDS,
)
from database import db_get_user, db_create_user, db_is_banned
from subscription import issue_subscription
from user import (
    kb_back, safe_edit, is_sub_active,
)

logger = logging.getLogger(__name__)
router = Router(name="payments")


# ── клавиатуры ───────────────────────────────────────

def kb_buy() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=f"⭐ Telegram Stars ({STARS_AMOUNT} ⭐)", callback_data="pay_stars"
        )],
        [types.InlineKeyboardButton(
            text=f"💳 СБП / Карта ({PRICE_RUB} ₽)", callback_data="pay_tribute"
        )],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


def kb_tribute_wait() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Я оплатил", callback_data="tribute_check")],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")],
    ])


# ══════════════════════════════════════════════════════
#  BUY
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    user = await db_get_user(call.from_user.id)
    has_sub = is_sub_active(user) if user else False
    if has_sub:
        expires = datetime.fromisoformat(user["subscription_expires"])
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


# ══════════════════════════════════════════════════════
#  STARS
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    await call.answer()
    await call.message.answer_invoice(
        title=f"AstroVPN — {SUBSCRIPTION_DAYS} дней",
        description=f"VPN доступ на {SUBSCRIPTION_DAYS} дней. VLESS + Reality.",
        payload=f"vpn_{SUBSCRIPTION_DAYS}d_{call.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"VPN {SUBSCRIPTION_DAYS} дней", amount=STARS_AMOUNT)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    tg_id = message.from_user.id
    charge_id = message.successful_payment.telegram_payment_charge_id
    logger.info(f"Stars оплата: tg_id={tg_id}, charge_id={charge_id}")

    user = await db_get_user(tg_id)
    if not user:
        await db_create_user(
            tg_id,
            message.from_user.full_name or "Пользователь",
            message.from_user.username,
        )

    await message.answer("✅ Оплата получена! Создаём ваш ключ...")
    ok = await issue_subscription(
        bot=message.bot, tg_id=tg_id, source="stars", charge_id=charge_id
    )
    if not ok:
        await message.answer(
            f"❌ Ошибка создания ключа. Напишите {SUPPORT_USERNAME}\n"
            f"Ваш ID: <code>{tg_id}</code>",
            parse_mode="HTML",
        )

    for admin_id in ADMIN_IDS:
        try:
            stars = message.successful_payment.total_amount
            await message.bot.send_message(
                admin_id,
                f"⭐ <b>Новая оплата Stars!</b>\n"
                f"👤 {message.from_user.full_name} (<code>{tg_id}</code>)\n"
                f"⭐ {stars} Stars",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════
#  TRIBUTE (callbacks)
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "pay_tribute")
async def cb_pay_tribute(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    tg_id = call.from_user.id
    await db_create_user(
        tg_id, call.from_user.full_name or "Пользователь", call.from_user.username
    )
    text = (
        "💳 <b>Оплата через СБП / Карту (Tribute)</b>\n\n"
        f"1️⃣ Перейдите по ссылке и оплатите <b>{PRICE_RUB} ₽</b>:\n"
        f"{TRIBUTE_LINK}\n\n"
        "2️⃣ После оплаты ключ придёт автоматически (до 1 мин)\n\n"
        "⚠️ <i>Не меняйте сумму — иначе оплата не зачтётся</i>"
    )
    await safe_edit(call.message, text, kb_tribute_wait())
    await call.answer()


@router.callback_query(F.data == "tribute_check")
async def cb_tribute_check(call: types.CallbackQuery):
    if await db_is_banned(call.from_user.id):
        return
    user = await db_get_user(call.from_user.id)
    if user and is_sub_active(user):
        expires = datetime.fromisoformat(user["subscription_expires"])
        await safe_edit(
            call.message,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Подписка активна до {expires.strftime('%d.%m.%Y')}\n\n"
            "Нажмите <b>🔑 Мой ключ</b> в главном меню.",
            kb_back(),
        )
    else:
        await safe_edit(
            call.message,
            "⏳ <b>Оплата ещё не подтверждена.</b>\n\n"
            "Подождите 1–2 минуты и нажмите снова.\n\n"
            f"Если проблема — напишите {SUPPORT_USERNAME}",
            kb_tribute_wait(),
        )
    await call.answer()


# ══════════════════════════════════════════════════════
#  TRIBUTE WEBHOOK (aiohttp)
# ══════════════════════════════════════════════════════

async def tribute_webhook_handler(request: web.Request, bot) -> web.Response:
    """
    Структура реального Tribute webhook:
    {
      "name": "new_donation",
      "payload": {
        "telegram_user_id": 123456789,
        "amount": 19900,   <- копейки
        "currency": "RUB",
        "id": "payment_id"
      }
    }
    Тестовый: {"test_event": "test_event"} — игнорируем.
    """
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
                    parse_mode="HTML",
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
                parse_mode="HTML",
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
        bot=bot, tg_id=tg_id, source="tribute", charge_id=tribute_payment_id
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
                parse_mode="HTML",
            )
        except Exception:
            pass

    return web.Response(status=200, text="OK")
