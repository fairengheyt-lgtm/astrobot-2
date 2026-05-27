"""
server_manager.py — выбор сервера, балансировка, alert админам при переполнении.
"""

import logging
from typing import Optional

from aiogram import Bot

from config import ADMIN_IDS
from database import (
    db_all_servers,
    db_count_active_subs_on_server,
    db_get_server,
)

logger = logging.getLogger(__name__)


async def get_server_with_load(server_id: int) -> Optional[dict]:
    """Возвращает сервер + поле current_clients."""
    srv = await db_get_server(server_id)
    if not srv:
        return None
    srv["current_clients"] = await db_count_active_subs_on_server(server_id)
    return srv


async def list_servers_with_load() -> list[dict]:
    """Все серверы + current_clients."""
    servers = await db_all_servers(only_enabled=False)
    for s in servers:
        s["current_clients"] = await db_count_active_subs_on_server(s["id"])
    return servers


async def get_available_server(bot: Optional[Bot] = None) -> Optional[dict]:
    """
    Выбирает enabled-сервер с current_clients < max_clients и наименьшей загрузкой.
    Если все полные — берёт наименее перегруженный + шлёт alert админам.
    Если ни одного включённого сервера нет — None.
    """
    servers = await db_all_servers(only_enabled=True)
    if not servers:
        logger.error("get_available_server: нет ни одного включённого сервера в БД")
        if bot:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        "🚨 <b>Нет включённых серверов!</b>\n\n"
                        "В БД нет ни одного активного сервера. "
                        "Добавьте через /server_add.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        return None

    # Загружаем нагрузку
    for s in servers:
        s["current_clients"] = await db_count_active_subs_on_server(s["id"])
        s["load_ratio"] = s["current_clients"] / max(1, s["max_clients"])

    # Сортируем по нагрузке (по возрастанию)
    servers.sort(key=lambda s: s["load_ratio"])

    available = [s for s in servers if s["current_clients"] < s["max_clients"]]
    if available:
        chosen = available[0]
        logger.info(
            f"Server selected: id={chosen['id']} '{chosen['name']}' "
            f"({chosen['current_clients']}/{chosen['max_clients']})"
        )
        return chosen

    # Все переполнены — берём наименее перегруженный + alert
    chosen = servers[0]
    logger.warning(
        f"Все серверы переполнены! Используем id={chosen['id']} '{chosen['name']}' "
        f"({chosen['current_clients']}/{chosen['max_clients']})"
    )
    if bot:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ <b>Все серверы переполнены!</b>\n\n"
                    f"Использован: <b>{chosen['name']}</b> "
                    f"({chosen['current_clients']}/{chosen['max_clients']}).\n\n"
                    f"Срочно добавьте новый сервер через /server_add",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить alert админу {admin_id}: {e}")
    return chosen
