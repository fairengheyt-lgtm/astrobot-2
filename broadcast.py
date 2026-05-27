"""
broadcast.py — массовые рассылки администратора.

Реализует три фильтра аудитории:
- all: все пользователи из users
- active: только активные подписки
- expired: только те, у кого подписка была, но истекла
"""

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from config import BROADCAST_DELAY_SECONDS
from database import db_all_users

logger = logging.getLogger(__name__)


def _is_active(user: dict) -> bool:
    raw = user.get("subscription_expires")
    if not raw or not user.get("vpn_uuid"):
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.utcnow()
    except ValueError:
        return False


async def filter_recipients(audience: str) -> list[int]:
    """audience ∈ {'all', 'active', 'expired'} → list of tg_id."""
    users = await db_all_users()
    if audience == "active":
        users = [u for u in users if _is_active(u)]
    elif audience == "expired":
        # Подписка БЫЛА (есть запись), но сейчас не активна.
        users = [u for u in users if not _is_active(u)]
    # 'all' → без фильтра
    return [u["tg_id"] for u in users]


async def send_broadcast(
    bot: Bot,
    audience: str,
    text: str,
    parse_mode: str = "HTML",
) -> tuple[int, int]:
    """
    Отправляет сообщение всем получателям с задержкой BROADCAST_DELAY_SECONDS.
    Возвращает (отправлено_успешно, ошибок).
    """
    recipients = await filter_recipients(audience)
    ok_count = 0
    err_count = 0

    for tg_id in recipients:
        try:
            await bot.send_message(tg_id, text, parse_mode=parse_mode)
            ok_count += 1
        except Exception as e:
            err_count += 1
            logger.debug(f"broadcast: ошибка для {tg_id}: {e}")
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    logger.info(
        f"broadcast '{audience}': отправлено={ok_count}, ошибок={err_count}, "
        f"всего={len(recipients)}"
    )
    return ok_count, err_count
