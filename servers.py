"""
handlers/servers.py — управление мультисерверной инфраструктурой через админ-команды.

/server_add — пошаговый FSM-диалог
/server_list — список серверов с загрузкой
/server_info <id>
/server_limit <id> <число>
/server_enable <id> / /server_disable <id>
/server_delete <id>
"""

import logging

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS, DEFAULT_SERVER_MAX_CLIENTS
from database import (
    db_get_server, db_create_server,
    db_set_server_limit, db_set_server_enabled, db_delete_server,
)
from server_manager import list_servers_with_load, get_server_with_load

logger = logging.getLogger(__name__)
router = Router(name="servers")


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


# ══════════════════════════════════════════════════════
#  /server_list
# ══════════════════════════════════════════════════════

@router.message(Command("server_list"))
async def cmd_server_list(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    servers = await list_servers_with_load()
    if not servers:
        await message.answer(
            "📭 Серверов нет.\n\nДобавьте через /server_add",
            parse_mode="HTML",
        )
        return

    lines = [f"🌍 <b>Серверы ({len(servers)})</b>", "━━━━━━━━━━━━━━━━━━"]
    for s in servers:
        status = "🟢 Активен" if s.get("enabled") else "🔴 Выключен"
        load = s["current_clients"]
        cap = s["max_clients"]
        pct = round(load / max(1, cap) * 100)
        api_p = s.get("api_port") or s.get("port") or "?"
        vpn_p = s.get("vpn_port") or "?"
        lines.append(
            f"\n<b>{s['name']}</b> (ID: {s['id']})\n"
            f"📍 {s['host']}\n"
            f"🔌 API: <code>{api_p}</code> | 🌐 VPN: <code>{vpn_p}</code>\n"
            f"👥 {load}/{cap} ({pct}%)\n"
            f"{status}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  /server_info <id>
# ══════════════════════════════════════════════════════

@router.message(Command("server_info"))
async def cmd_server_info(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /server_info &lt;id&gt;", parse_mode="HTML")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    s = await get_server_with_load(sid)
    if not s:
        await message.answer(f"Сервер id={sid} не найден.")
        return

    status = "🟢 Активен" if s.get("enabled") else "🔴 Выключен"
    api_p = s.get("api_port") or s.get("port") or "?"
    vpn_p = s.get("vpn_port") or "?"
    text = (
        f"🌍 <b>{s['name']}</b> (ID: {s['id']})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 Host: <code>{s['host']}</code>\n"
        f"🔌 API порт (панель 3X-UI): <code>{api_p}</code>\n"
        f"🌐 VPN порт (для ссылки клиенту): <code>{vpn_p}</code>\n"
        f"🛤 Base path: <code>{s.get('base_path') or '—'}</code>\n"
        f"🔑 API token: <code>{'есть' if s.get('api_token') else '—'}</code>\n"
        f"📡 Inbound ID: <code>{s['inbound_id']}</code>\n"
        f"🔐 Public key: <code>{(s.get('public_key') or '—')[:24]}...</code>\n"
        f"🆔 Short ID: <code>{s.get('short_id') or '—'}</code>\n"
        f"🌐 SNI: <code>{s.get('sni')}</code>\n"
        f"✏️ Fingerprint: <code>{s.get('fingerprint')}</code>\n"
        f"➡️ Flow: <code>{s.get('flow')}</code>\n"
        f"🧬 Protocol: <code>{s.get('protocol')}</code>\n\n"
        f"👥 Клиентов: <b>{s['current_clients']}/{s['max_clients']}</b>\n"
        f"{status}\n\n"
        f"📅 Создан: {s.get('created_at','—')}"
    )
    await message.answer(text, parse_mode="HTML")


# ══════════════════════════════════════════════════════
#  /server_limit <id> <n>
# ══════════════════════════════════════════════════════

@router.message(Command("server_limit"))
async def cmd_server_limit(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /server_limit &lt;id&gt; &lt;число&gt;", parse_mode="HTML")
        return
    try:
        sid = int(parts[1])
        limit = int(parts[2])
    except ValueError:
        await message.answer("id и число должны быть целыми")
        return
    if limit < 1:
        await message.answer("Лимит должен быть ≥ 1")
        return
    ok = await db_set_server_limit(sid, limit)
    if ok:
        await message.answer(f"✅ Лимит сервера {sid} установлен в <b>{limit}</b>", parse_mode="HTML")
    else:
        await message.answer(f"Сервер {sid} не найден.")


# ══════════════════════════════════════════════════════
#  /server_enable /server_disable /server_delete
# ══════════════════════════════════════════════════════

@router.message(Command("server_enable"))
async def cmd_server_enable(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /server_enable &lt;id&gt;", parse_mode="HTML")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    if await db_set_server_enabled(sid, True):
        await message.answer(f"🟢 Сервер {sid} включён.")
    else:
        await message.answer(f"Сервер {sid} не найден.")


@router.message(Command("server_disable"))
async def cmd_server_disable(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /server_disable &lt;id&gt;", parse_mode="HTML")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    if await db_set_server_enabled(sid, False):
        await message.answer(f"🔴 Сервер {sid} выключен (новые ключи туда не пойдут).")
    else:
        await message.answer(f"Сервер {sid} не найден.")


@router.message(Command("server_delete"))
async def cmd_server_delete(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /server_delete &lt;id&gt;", parse_mode="HTML")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    if await db_delete_server(sid):
        await message.answer(
            f"🗑 Сервер {sid} удалён из БД.\n"
            "⚠️ <i>Клиенты в 3X-UI не тронуты — удалите вручную при необходимости.</i>",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"Сервер {sid} не найден.")


# ══════════════════════════════════════════════════════
#  /server_add — FSM
# ══════════════════════════════════════════════════════

class ServerAddSG(StatesGroup):
    name = State()
    host = State()
    api_port = State()       # порт панели 3X-UI (для API запросов)
    vpn_port = State()       # порт VLESS-подключения (для ссылки клиенту)
    base_path = State()
    api_token = State()
    inbound_id = State()
    public_key = State()
    short_id = State()
    sni = State()
    max_clients = State()
    confirm = State()


def _kb_skip() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⏭ Пропустить", callback_data="srv_skip")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="srv_cancel")],
    ])


def _kb_cancel() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="srv_cancel")]
    ])


def _kb_confirm() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Создать", callback_data="srv_create"),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data="srv_cancel"),
        ]
    ])


@router.message(Command("server_add"))
async def cmd_server_add(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(ServerAddSG.name)
    await message.answer(
        "➕ <b>Добавление сервера</b>\n\n"
        "Шаг 1/11. Введите <b>название</b> сервера (например: Нидерланды):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "srv_cancel")
async def cb_srv_cancel(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    try:
        await call.message.edit_text("❌ Добавление сервера отменено.", parse_mode="HTML")
    except Exception:
        pass
    await call.answer()


@router.message(ServerAddSG.name)
async def srv_step_name(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(name=message.text.strip()[:80])
    await state.set_state(ServerAddSG.host)
    await message.answer(
        "Шаг 2/11. Введите <b>host</b> (например: <code>https://89.127.207.207</code> или <code>http://1.2.3.4</code>):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.host)
async def srv_step_host(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    host = message.text.strip()
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"
    await state.update_data(host=host)
    await state.set_state(ServerAddSG.api_port)
    await message.answer(
        "Шаг 3/11. Введите порт <b>ПАНЕЛИ 3X-UI</b> "
        "(для API-запросов, например <code>33758</code>):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.api_port)
async def srv_step_api_port(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        port = int(message.text.strip())
    except ValueError:
        await message.answer("API-порт должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(api_port=port)
    await state.set_state(ServerAddSG.vpn_port)
    await message.answer(
        "Шаг 4/11. Введите порт <b>VPN-подключения</b> "
        "(для ссылки клиенту, обычно <code>443</code>):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.vpn_port)
async def srv_step_vpn_port(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        port = int(message.text.strip())
    except ValueError:
        await message.answer("VPN-порт должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(vpn_port=port)
    await state.set_state(ServerAddSG.base_path)
    await message.answer(
        "Шаг 5/11. Введите <b>base path</b> (или нажмите «Пропустить» если его нет):",
        reply_markup=_kb_skip(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.base_path)
async def srv_step_base_path(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(base_path=message.text.strip())
    await state.set_state(ServerAddSG.api_token)
    await message.answer(
        "Шаг 6/11. Введите <b>API token</b> (Bearer-токен 3X-UI):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "srv_skip", ServerAddSG.base_path)
async def srv_skip_base_path(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.update_data(base_path="")
    await state.set_state(ServerAddSG.api_token)
    await call.message.answer(
        "Шаг 6/11. Введите <b>API token</b> (Bearer-токен 3X-UI):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(ServerAddSG.api_token)
async def srv_step_api_token(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(api_token=message.text.strip())
    await state.set_state(ServerAddSG.inbound_id)
    await message.answer(
        "Шаг 7/11. Введите <b>Inbound ID</b> (например: <code>1</code>):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.inbound_id)
async def srv_step_inbound(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        inbound_id = int(message.text.strip())
    except ValueError:
        await message.answer("Inbound ID должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(inbound_id=inbound_id)
    await state.set_state(ServerAddSG.public_key)
    await message.answer(
        "Шаг 8/11. Введите <b>Public Key</b> (Reality pbk):",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.public_key)
async def srv_step_pubkey(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(public_key=message.text.strip())
    await state.set_state(ServerAddSG.short_id)
    await message.answer(
        "Шаг 9/11. Введите <b>Short ID</b>:",
        reply_markup=_kb_cancel(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.short_id)
async def srv_step_shortid(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(short_id=message.text.strip())
    await state.set_state(ServerAddSG.sni)
    await message.answer(
        "Шаг 10/11. Введите <b>SNI</b> (по умолчанию: <code>www.apple.com</code>):",
        reply_markup=_kb_skip(),
        parse_mode="HTML",
    )


@router.message(ServerAddSG.sni)
async def srv_step_sni(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    sni = message.text.strip() or "www.apple.com"
    await state.update_data(sni=sni)
    await state.set_state(ServerAddSG.max_clients)
    await message.answer(
        f"Шаг 11/11. Введите <b>лимит клиентов</b> (по умолчанию: <code>{DEFAULT_SERVER_MAX_CLIENTS}</code>):",
        reply_markup=_kb_skip(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "srv_skip", ServerAddSG.sni)
async def srv_skip_sni(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.update_data(sni="www.apple.com")
    await state.set_state(ServerAddSG.max_clients)
    await call.message.answer(
        f"Шаг 11/11. Введите <b>лимит клиентов</b> (по умолчанию: <code>{DEFAULT_SERVER_MAX_CLIENTS}</code>):",
        reply_markup=_kb_skip(),
        parse_mode="HTML",
    )
    await call.answer()


async def _show_summary(target, state: FSMContext):
    data = await state.get_data()
    text = (
        "📋 <b>Проверьте данные сервера:</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📝 Имя: <b>{data.get('name')}</b>\n"
        f"📍 Host: <code>{data.get('host')}</code>\n"
        f"🔌 API порт (панель): <code>{data.get('api_port')}</code>\n"
        f"🌐 VPN порт (для ссылки): <code>{data.get('vpn_port')}</code>\n"
        f"🛤 Base path: <code>{data.get('base_path') or '—'}</code>\n"
        f"🔑 API token: <code>{'есть' if data.get('api_token') else '—'}</code>\n"
        f"📡 Inbound ID: <code>{data.get('inbound_id')}</code>\n"
        f"🔐 Public key: <code>{(data.get('public_key','') or '')[:24]}...</code>\n"
        f"🆔 Short ID: <code>{data.get('short_id')}</code>\n"
        f"🌐 SNI: <code>{data.get('sni')}</code>\n"
        f"👥 Лимит: <b>{data.get('max_clients')}</b>"
    )
    await target.answer(text, reply_markup=_kb_confirm(), parse_mode="HTML")


@router.message(ServerAddSG.max_clients)
async def srv_step_limit(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    try:
        limit = int(raw) if raw else DEFAULT_SERVER_MAX_CLIENTS
    except ValueError:
        await message.answer("Лимит должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(max_clients=limit)
    await state.set_state(ServerAddSG.confirm)
    await _show_summary(message, state)


@router.callback_query(F.data == "srv_skip", ServerAddSG.max_clients)
async def srv_skip_limit(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.update_data(max_clients=DEFAULT_SERVER_MAX_CLIENTS)
    await state.set_state(ServerAddSG.confirm)
    await _show_summary(call.message, state)
    await call.answer()


@router.callback_query(F.data == "srv_create", ServerAddSG.confirm)
async def srv_create(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    try:
        new_id = await db_create_server(data)
        logger.info(f"Создан сервер id={new_id} '{data.get('name')}' админом {call.from_user.id}")
        try:
            await call.message.edit_text(
                f"✅ <b>Сервер создан!</b>\n\n"
                f"ID: <code>{new_id}</code>\n"
                f"Имя: <b>{data.get('name')}</b>\n\n"
                "Сервер активен. Новые ключи могут создаваться на нём.",
                parse_mode="HTML",
            )
        except Exception:
            await call.message.answer(f"✅ Сервер создан, ID: {new_id}")
    except Exception as e:
        logger.error(f"Ошибка создания сервера: {e}")
        try:
            await call.message.edit_text(f"❌ Ошибка создания: {e}", parse_mode="HTML")
        except Exception:
            await call.message.answer(f"❌ Ошибка создания: {e}")
    await call.answer()
