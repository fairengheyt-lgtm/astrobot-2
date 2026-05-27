"""
notifications.py — авто-напоминания и проверка истёкших подписок.

Два scheduler-job'а:
- check_expired_subscriptions: каждые 10 мин, отключает истёкшие ключи в 3X-UI
- reminders_job: раз в час, шлёт напоминания за 3д/1д до истечения и через 1д/7д после
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot

from database import (
    db_active_subs, db_revoke, db_all_users,
    db_get_server, db_set_notified_flag, db_last_subscription_end,
)
from xui_client import xui_disable_client

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
#  ПРОВЕРКА ИСТЁКШИХ
# ══════════════════════════════════════════════════════

async def check_expired_subscriptions(bot: Bot):
    """Каждые 10 минут: отключает в 3X-UI клиентов с истёкшей подпиской."""
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
            server = await db_get_server(user.get("server_id") or 1)
            if vpn_uuid and server:
                try:
                    await xui_disable_client(server, vpn_uuid, tg_id)
                except Exception as e:
                    logger.warning(f"check_expired: ошибка xui_disable для {tg_id}: {e}")
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
#  АВТО-НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════

MSG_3D_BEFORE = (
    "⏰ <b>Подписка скоро закончится</b>\n\n"
    "Привет! Ваша подписка AstroVPN истекает через 3 дня — <b>{date}</b>.\n\n"
    "🚀 Продлите сейчас — нажмите /start и выберите «Продлить»\n\n"
    "Не пропустите ни одного фильма, видео и сообщения 🌐"
)

MSG_1D_BEFORE = (
    "⚠️ <b>Завтра подписка истекает!</b>\n\n"
    "Завтра ваш доступ к AstroVPN отключится.\n\n"
    "Продлите за 1 минуту: /start → «Продлить»\n\n"
    "Не теряйте связь с миром 🌍"
)

MSG_1D_AFTER = (
    "😢 <b>Скучаем по вам!</b>\n\n"
    "Ваша подписка AstroVPN закончилась.\n\n"
    "Возвращайтесь — мы вас ждём 💙\n\n"
    "Нажмите /start чтобы оформить заново"
)

MSG_7D_AFTER = (
    "🎁 <b>Всё ещё ждём вас!</b>\n\n"
    "Уже неделя как вы без VPN. Скучаем!\n\n"
    "Нажмите /start и получите доступ за минуту 🚀"
)


async def _safe_send(bot: Bot, tg_id: int, text: str) -> bool:
    try:
        await bot.send_message(tg_id, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"reminders: не удалось отправить tg_id={tg_id}: {e}")
        return False


async def reminders_job(bot: Bot):
    """Раз в час: проверяем кому пора напомнить."""
    logger.debug("Scheduler: проверка напоминаний...")
    now = datetime.utcnow()
    users = await db_all_users()

    sent_count = 0

    for u in users:
        tg_id = u["tg_id"]
        expires_raw = u.get("subscription_expires")
        vpn_uuid = u.get("vpn_uuid")

        # ── напоминания ДО истечения (только активные) ──
        if expires_raw and vpn_uuid:
            try:
                expires = datetime.fromisoformat(expires_raw)
            except ValueError:
                expires = None

            if expires and expires > now:
                delta = expires - now

                # за 3 дня (попадаем в окно 2..3 дня — раз в час сработает один раз)
                if (
                    timedelta(days=2) < delta <= timedelta(days=3)
                    and not u.get("notified_3d_before")
                ):
                    if await _safe_send(
                        bot, tg_id, MSG_3D_BEFORE.format(date=expires.strftime("%d.%m.%Y"))
                    ):
                        await db_set_notified_flag(tg_id, "notified_3d_before")
                        sent_count += 1
                    continue

                # за 1 день
                if (
                    timedelta(0) < delta <= timedelta(days=1)
                    and not u.get("notified_1d_before")
                ):
                    if await _safe_send(bot, tg_id, MSG_1D_BEFORE):
                        await db_set_notified_flag(tg_id, "notified_1d_before")
                        sent_count += 1
                    continue

        # ── напоминания ПОСЛЕ истечения (vpn_uuid уже NULL после revoke) ──
        # Дата окончания берётся из последней записи в subscriptions.
        if not vpn_uuid:
            last_end = await db_last_subscription_end(tg_id)
            if not last_end:
                continue
            elapsed = now - last_end
            if elapsed < timedelta(0):
                continue  # подписка ещё не истекла, странный случай

            # через 1 день после
            if (
                timedelta(hours=20) <= elapsed <= timedelta(days=2)
                and not u.get("notified_1d_after")
            ):
                if await _safe_send(bot, tg_id, MSG_1D_AFTER):
                    await db_set_notified_flag(tg_id, "notified_1d_after")
                    sent_count += 1
                continue

            # через 7 дней после
            if (
                timedelta(days=7) <= elapsed <= timedelta(days=8)
                and not u.get("notified_7d_after")
            ):
                if await _safe_send(bot, tg_id, MSG_7D_AFTER):
                    await db_set_notified_flag(tg_id, "notified_7d_after")
                    sent_count += 1
                continue

    if sent_count:
        logger.info(f"reminders_job: отправлено {sent_count} напоминаний")
