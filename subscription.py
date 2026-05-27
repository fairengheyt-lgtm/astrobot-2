"""
subscription.py — центральная функция выдачи/продления подписки + формирование чека.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot

from config import (
    SUBSCRIPTION_DAYS,
    STARS_AMOUNT, PRICE_RUB,
    SUPPORT_USERNAME,
)
from database import (
    db_get_user, db_get_server,
    db_set_subscription, db_log_subscription,
)
from server_manager import get_available_server
from xui_client import (
    xui_create_client, xui_update_client_expire, build_vless_link,
)

logger = logging.getLogger(__name__)


def build_receipt(
    source: str, start: datetime, end: datetime, vless: str,
    days: int = SUBSCRIPTION_DAYS, extra_note: Optional[str] = None,
) -> str:
    if source == "stars":
        method = f"⭐ Telegram Stars ({STARS_AMOUNT} ⭐)"
    elif source == "admin":
        method = "👤 Выдан администратором"
    elif source == "referral":
        method = "🎁 Реферальный бонус"
    else:
        method = f"💳 Tribute / СБП ({PRICE_RUB} ₽)"

    note = ""
    if extra_note:
        note = f"\n{extra_note}\n"

    return (
        "🧾 <b>ЧЕК ОБ ОПЛАТЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Товар: VPN доступ на {days} дней\n"
        f"💳 Способ: {method}\n"
        f"📅 Дата: {start.strftime('%d.%m.%Y')}\n"
        f"⏳ Истекает: {end.strftime('%d.%m.%Y')}\n"
        "━━━━━━━━━━━━━━━━━━"
        f"{note}\n"
        "🔑 Ваш ключ:\n"
        f"<code>{vless}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Нажмите на ключ чтобы скопировать\n"
        "📖 Как подключить — /guide"
    )


async def issue_subscription(
    bot: Bot,
    tg_id: int,
    source: str,
    charge_id: Optional[str] = None,
    days_override: Optional[int] = None,
    send_receipt: bool = True,
) -> bool:
    """
    Создаёт/продлевает VPN клиента и сохраняет в БД, отправляет чек.

    source: 'stars' | 'tribute' | 'admin' | 'referral'
    days_override: для реферального бонуса (например 7) — иначе используется SUBSCRIPTION_DAYS
    send_receipt: если False — чек не шлём (используется когда вызывающая сторона хочет своё уведомление)
    """
    # Импортируем тут, чтобы избежать кругового импорта (referral → subscription)
    from referral import grant_referral_bonus

    days = days_override if days_override is not None else SUBSCRIPTION_DAYS

    user = await db_get_user(tg_id)
    if not user:
        logger.error(f"issue_subscription: пользователь {tg_id} не найден")
        return False

    now = datetime.utcnow()
    end = now + timedelta(days=days)

    # Продление от даты истечения (если активна)
    existing_expires_raw = user.get("subscription_expires")
    if existing_expires_raw:
        try:
            existing_expires = datetime.fromisoformat(existing_expires_raw)
            if existing_expires > now:
                end = existing_expires + timedelta(days=days)
        except ValueError:
            pass

    # Определяем сервер
    existing_uuid = user.get("vpn_uuid")
    server_id_for_user: Optional[int] = None

    if existing_uuid:
        # Уже есть ключ — используем тот же сервер
        srv_id = user.get("server_id") or 1
        server = await db_get_server(srv_id)
        if not server:
            # сервер удалён — выбираем доступный
            server = await get_available_server(bot)
            if not server:
                try:
                    await bot.send_message(
                        tg_id,
                        f"❌ Ошибка: нет доступных серверов. Напишите {SUPPORT_USERNAME}",
                    )
                except Exception:
                    pass
                return False
            server_id_for_user = server["id"]

        # Пробуем обновить
        ok = await xui_update_client_expire(server, existing_uuid, tg_id, end)
        vpn_uuid = existing_uuid
        if not ok:
            # Пересоздаём
            new_uuid = await xui_create_client(
                server, tg_id, user.get("name", f"user_{tg_id}"), end
            )
            if not new_uuid:
                try:
                    await bot.send_message(
                        tg_id,
                        f"❌ Ошибка обновления ключа. Напишите {SUPPORT_USERNAME}",
                    )
                except Exception:
                    pass
                return False
            vpn_uuid = new_uuid
            server_id_for_user = server["id"]
    else:
        # Новый ключ — выбираем сервер с балансировкой
        server = await get_available_server(bot)
        if not server:
            try:
                await bot.send_message(
                    tg_id,
                    f"❌ Ошибка: нет доступных серверов. Напишите {SUPPORT_USERNAME}",
                )
            except Exception:
                pass
            return False

        vpn_uuid = await xui_create_client(
            server, tg_id, user.get("name", f"user_{tg_id}"), end
        )
        if not vpn_uuid:
            try:
                await bot.send_message(
                    tg_id,
                    f"❌ Ошибка создания ключа. Напишите {SUPPORT_USERNAME}",
                )
            except Exception:
                pass
            return False
        server_id_for_user = server["id"]

    # Сохраняем в БД + сбрасываем флаги уведомлений
    await db_set_subscription(tg_id, vpn_uuid, end, server_id=server_id_for_user)
    await db_log_subscription(tg_id, now, end, source, charge_id)

    # Чек
    vless = build_vless_link(vpn_uuid, server)
    if send_receipt:
        extra_note = None
        if source == "referral":
            extra_note = "🎁 <i>Это реферальный бонус за приглашённого друга!</i>"
        receipt = build_receipt(source, now, end, vless, days=days, extra_note=extra_note)
        try:
            await bot.send_message(tg_id, receipt, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить чек {tg_id}: {e}")

    logger.info(
        f"Подписка выдана: tg_id={tg_id}, source={source}, "
        f"uuid={vpn_uuid}, server={server_id_for_user}, до={end}"
    )

    # Реферальный бонус — только для платных source (не для referral, чтобы не зациклить)
    if source in ("stars", "tribute", "admin"):
        referrer_id = user.get("referred_by")
        if referrer_id and referrer_id != tg_id:
            try:
                await grant_referral_bonus(bot, int(referrer_id), tg_id)
            except Exception as e:
                logger.error(f"Реф. бонус: ошибка для referrer={referrer_id}: {e}")

    return True
