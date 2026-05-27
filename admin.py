"""
handlers/admin.py — административные команды.

/admin /users /keys /give /addkey /revoke
/stats /user_info /revenue
/ban /unban
/broadcast /broadcast_active /broadcast_expired
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS, SUBSCRIPTION_DAYS, PRICE_RUB, STARS_AMOUNT, SUPPORT_USERNAME
from database import (
    db_all_users, db_active_subs, db_get_user, db_get_server,
    db_user_subscriptions, db_count_user_subscriptions, db_revenue_since,
    db_revoke, db_ban_user, db_unban_user, db_is_banned,
    db_count_referrals, db_total_referrals_count,
)
from xui_client import xui_disable_client
from subscription import issue_subscription
from broadcast import send_broadcast, filter_recipients

logger = logging.getLogger(__name__)
router = Router(name="admin")


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def _is_active(user: dict) -> bool:
    raw = user.get("subscription_expires")
    if not raw or not user.get("vpn_uuid"):
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.utcnow()
    except ValueError:
        return False


# ══════════════════════════════════════════════════════
#  /admin
# ══════════════════════════════════════════════════════

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "🛠 <b>Команды администратора</b>\n\n"
        "<b>Пользователи:</b>\n"
        "/users — список пользователей\n"
        "/user_info &lt;id&gt; — детали пользователя\n"
        "/keys — активные ключи\n"
        "/give &lt;id&gt; — выдать подписку\n"
        "/addkey &lt;id&gt; — alias для /give\n"
        "/revoke &lt;id&gt; — отозвать подписку\n"
        "/ban &lt;id&gt; — забанить\n"
        "/unban &lt;id&gt; — разбанить\n\n"
        "<b>Статистика:</b>\n"
        "/stats — общая статистика\n"
        "/revenue — доход\n\n"
        "<b>Рассылки:</b>\n"
        "/broadcast &lt;текст&gt; — всем\n"
        "/broadcast_active &lt;текст&gt; — активным\n"
        "/broadcast_expired &lt;текст&gt; — истёкшим\n\n"
        "<b>Серверы:</b>\n"
        "/server_list — список\n"
        "/server_add — добавить\n"
        "/server_info &lt;id&gt; — детали\n"
        "/server_limit &lt;id&gt; &lt;n&gt; — лимит\n"
        "/server_enable &lt;id&gt; — включить\n"
        "/server_disable &lt;id&gt; — выключить\n"
        "/server_delete &lt;id&gt; — удалить"
    )
    await message.answer(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  /users /keys /give /addkey /revoke
# ══════════════════════════════════════════════════════

@router.message(Command("users"))
async def cmd_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await db_all_users()
    if not users:
        await message.answer("Пользователей нет.")
        return
    active = sum(1 for u in users if _is_active(u))
    lines = [f"👥 <b>Пользователи ({len(users)}):</b>"]
    for u in users[:50]:
        status = "✅" if _is_active(u) else "❌"
        lines.append(f"{status} <code>{u['tg_id']}</code> — {u.get('name','?')}")
    lines.append(f"\n📊 Активных: {active}/{len(users)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("keys"))
async def cmd_keys(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await db_active_subs()
    active = [u for u in users if _is_active(u)]
    if not active:
        await message.answer("Нет активных ключей.")
        return
    lines = [f"🔑 <b>Активные ключи ({len(active)}):</b>"]
    for u in active[:30]:
        raw = u.get("subscription_expires", "")
        exp_str = raw[:10] if raw else "?"
        uuid_short = (u.get("vpn_uuid") or "")[:18]
        srv = u.get("server_id") or 1
        lines.append(
            f"<code>{u['tg_id']}</code> до {exp_str} (srv {srv})\n"
            f"  <code>{uuid_short}...</code>"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("give"))
@router.message(Command("addkey"))
async def cmd_give(message: types.Message):
    if not is_admin(message.from_user.id):
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


@router.message(Command("revoke"))
async def cmd_revoke(message: types.Message):
    if not is_admin(message.from_user.id):
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
        server = await db_get_server(user.get("server_id") or 1)
        if server:
            try:
                await xui_disable_client(server, vpn_uuid, target_id)
            except Exception as e:
                logger.warning(f"revoke: xui_disable failed: {e}")
    await db_revoke(target_id)
    await message.answer(f"✅ Подписка отозвана у <code>{target_id}</code>", parse_mode="HTML")
    try:
        await message.bot.send_message(
            target_id,
            "⚠️ Ваша подписка AstroVPN была отозвана администратором.\n\n"
            "Для продления напишите /start",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  /stats /user_info /revenue
# ══════════════════════════════════════════════════════

@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await db_all_users()
    total = len(users)
    active = sum(1 for u in users if _is_active(u))
    expired = total - active

    month_ago = datetime.utcnow() - timedelta(days=30)
    month_payments = await db_revenue_since(month_ago)
    revenue_month = month_payments * PRICE_RUB
    total_refs = await db_total_referrals_count()

    text = (
        "📊 <b>Статистика AstroVPN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"✅ Активных подписок: <b>{active}</b>\n"
        f"❌ Без активной подписки: <b>{expired}</b>\n\n"
        f"💰 Доход за месяц (по оплатам): ~<b>{revenue_month} ₽</b>\n"
        f"   ({month_payments} платежей × {PRICE_RUB} ₽)\n\n"
        f"🎁 Всего приглашённых рефералов: <b>{total_refs}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("user_info"))
async def cmd_user_info(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /user_info &lt;user_id&gt;", parse_mode="HTML")
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

    name = user.get("name") or "—"
    username = user.get("username")
    username_str = f"@{username}" if username else "—"
    created = user.get("created_at") or "—"

    if _is_active(user):
        expires = datetime.fromisoformat(user["subscription_expires"])
        sub_text = f"✅ Активна до {expires.strftime('%d.%m.%Y')}"
    else:
        sub_text = "❌ Нет активной"

    server_id = user.get("server_id") or 1
    server = await db_get_server(server_id)
    server_name = server["name"] if server else f"id={server_id} (удалён)"

    refs_count = await db_count_referrals(target_id)
    subs_count = await db_count_user_subscriptions(target_id)
    last_subs = await db_user_subscriptions(target_id, limit=5)

    lines = [
        "👤 <b>Информация о пользователе</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 ID: <code>{target_id}</code>",
        f"👤 Имя: {name}",
        f"💬 Username: {username_str}",
        f"📅 Регистрация: {created[:10]}",
        "",
        f"📡 Подписка: {sub_text}",
        f"🌍 Сервер: {server_name}",
        "",
        f"🎁 Приглашённых: <b>{refs_count}</b>",
        f"💳 Всего платежей: <b>{subs_count}</b>",
    ]

    if last_subs:
        lines.append("\n<b>Последние платежи:</b>")
        for s in last_subs:
            lines.append(
                f"• {s.get('created_at','?')[:10]} — {s.get('source','?')} "
                f"(до {s.get('end_date','?')[:10]})"
            )

    if await db_is_banned(target_id):
        lines.append("\n🚫 <b>ЗАБАНЕН</b>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("revenue"))
async def cmd_revenue(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    now = datetime.utcnow()
    day_payments = await db_revenue_since(now - timedelta(days=1))
    week_payments = await db_revenue_since(now - timedelta(days=7))
    month_payments = await db_revenue_since(now - timedelta(days=30))
    total_payments = await db_revenue_since(datetime(2000, 1, 1))

    text = (
        "💰 <b>Доход AstroVPN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 За сегодня: <b>{day_payments * PRICE_RUB} ₽</b> ({day_payments} платежей)\n"
        f"📆 За неделю: <b>{week_payments * PRICE_RUB} ₽</b> ({week_payments} платежей)\n"
        f"📊 За месяц: <b>{month_payments * PRICE_RUB} ₽</b> ({month_payments} платежей)\n"
        f"🏆 Всего: <b>{total_payments * PRICE_RUB} ₽</b> ({total_payments} платежей)\n\n"
        f"<i>Все суммы по тарифу {PRICE_RUB} ₽ / {STARS_AMOUNT} ⭐</i>"
    )
    await message.answer(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  /ban /unban
# ══════════════════════════════════════════════════════

@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /ban &lt;user_id&gt; [причина]", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return
    reason = parts[2] if len(parts) > 2 else None
    await db_ban_user(target_id, reason)
    await message.answer(f"🚫 Пользователь <code>{target_id}</code> забанен.", parse_mode="HTML")
    logger.info(f"Admin {message.from_user.id} забанил {target_id} (reason={reason})")


@router.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /unban &lt;user_id&gt;", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return
    ok = await db_unban_user(target_id)
    if ok:
        await message.answer(f"✅ Пользователь <code>{target_id}</code> разбанен.", parse_mode="HTML")
    else:
        await message.answer(f"Пользователь <code>{target_id}</code> не был забанен.", parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  BROADCAST с подтверждением через FSM
# ══════════════════════════════════════════════════════

class BroadcastSG(StatesGroup):
    waiting_confirm = State()


def _kb_broadcast_confirm() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Отправить", callback_data="bc_confirm"),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel"),
        ]
    ])


async def _start_broadcast(message: types.Message, state: FSMContext, audience: str):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: <code>/broadcast Текст сообщения</code>\n\n"
            "Поддерживается HTML: &lt;b&gt;, &lt;i&gt;, &lt;a&gt; и т.д.",
            parse_mode="HTML",
        )
        return

    text = parts[1]
    recipients = await filter_recipients(audience)

    audience_label = {
        "all": "ВСЕМ пользователям",
        "active": "пользователям с АКТИВНОЙ подпиской",
        "expired": "пользователям с НЕАКТИВНОЙ подпиской",
    }.get(audience, audience)

    preview = (
        f"📣 <b>Превью рассылки</b> ({audience_label})\n"
        f"👥 Получателей: <b>{len(recipients)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Подтвердите отправку:"
    )

    await state.set_state(BroadcastSG.waiting_confirm)
    await state.update_data(text=text, audience=audience, recipients_count=len(recipients))
    await message.answer(preview, reply_markup=_kb_broadcast_confirm(), parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    await _start_broadcast(message, state, "all")


@router.message(Command("broadcast_active"))
async def cmd_broadcast_active(message: types.Message, state: FSMContext):
    await _start_broadcast(message, state, "active")


@router.message(Command("broadcast_expired"))
async def cmd_broadcast_expired(message: types.Message, state: FSMContext):
    await _start_broadcast(message, state, "expired")


@router.callback_query(F.data == "bc_confirm", BroadcastSG.waiting_confirm)
async def cb_broadcast_confirm(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    data = await state.get_data()
    text = data.get("text")
    audience = data.get("audience")
    count = data.get("recipients_count", 0)
    await state.clear()

    if not text or not audience:
        await call.answer("Сессия рассылки потеряна.", show_alert=True)
        return

    try:
        await call.message.edit_text(
            f"📤 Отправка началась... ({count} получателей)",
            parse_mode="HTML",
        )
    except Exception:
        pass

    ok_count, err_count = await send_broadcast(call.bot, audience, text, parse_mode="HTML")

    result_text = (
        "📊 <b>Результат рассылки</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"✅ Отправлено: <b>{ok_count}</b>\n"
        f"❌ Ошибок: <b>{err_count}</b>\n"
        f"👥 Всего: <b>{ok_count + err_count}</b>"
    )
    try:
        await call.message.edit_text(result_text, parse_mode="HTML")
    except Exception:
        await call.message.answer(result_text, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "bc_cancel", BroadcastSG.waiting_confirm)
async def cb_broadcast_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Рассылка отменена.", parse_mode="HTML")
    except Exception:
        pass
    await call.answer("Отменено")
