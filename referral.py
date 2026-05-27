"""
referral.py — реферальный бонус.

ФИКС: если у пригласившего НЕТ активной подписки — создаём реальный VPN-ключ
на REFERRAL_BONUS_DAYS дней через issue_subscription, а не просто продляем
несуществующую запись.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot

from config import REFERRAL_BONUS_DAYS
from database import (
    db_get_user, db_get_server,
    db_extend_subscription_expires,
)
from xui_client import xui_update_client_expire

logger = logging.getLogger(__name__)


def _is_sub_active(user: dict) -> bool:
    raw = user.get("subscription_expires")
    if not raw or not user.get("vpn_uuid"):
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.utcnow()
    except ValueError:
        return False


async def grant_referral_bonus(bot: Bot, referrer_id: int, referred_id: int):
    """Начисляет referrer'у +REFERRAL_BONUS_DAYS дней."""
    referrer = await db_get_user(referrer_id)
    if not referrer:
        logger.warning(f"Реф. бонус: referrer {referrer_id} не найден")
        return

    if _is_sub_active(referrer):
        # Активная подписка → продлеваем +7 дней
        now = datetime.utcnow()
        try:
            current = datetime.fromisoformat(referrer["subscription_expires"])
        except ValueError:
            current = now
        base = current if current > now else now
        new_expires = base + timedelta(days=REFERRAL_BONUS_DAYS)

        vpn_uuid = referrer.get("vpn_uuid")
        server = await db_get_server(referrer.get("server_id") or 1)
        if vpn_uuid and server:
            try:
                await xui_update_client_expire(server, vpn_uuid, referrer_id, new_expires)
            except Exception as e:
                logger.warning(f"Реф. бонус: 3X-UI update не удался для {referrer_id}: {e}")

        await db_extend_subscription_expires(referrer_id, new_expires)

        try:
            await bot.send_message(
                referrer_id,
                "🎁 <b>Реферальный бонус!</b>\n\n"
                "По вашей ссылке оплатил новый пользователь.\n"
                f"Вам начислено <b>+{REFERRAL_BONUS_DAYS} дней</b> к подписке ⭐\n\n"
                f"📅 Подписка теперь до: <b>{new_expires.strftime('%d.%m.%Y')}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Реф. бонус: не удалось уведомить {referrer_id}: {e}")

        logger.info(
            f"Реф. бонус [extend]: tg_id={referrer_id} +{REFERRAL_BONUS_DAYS}д "
            f"за tg_id={referred_id}, до={new_expires.isoformat()}"
        )
    else:
        # Нет активной подписки → создаём реальный VPN-ключ на 7 дней
        # Импортим тут чтобы избежать кругового импорта
        from subscription import issue_subscription

        try:
            await bot.send_message(
                referrer_id,
                "🎁 <b>Реферальный бонус!</b>\n\n"
                "По вашей ссылке оплатил новый пользователь.\n"
                f"Вам начислен <b>VPN-ключ на {REFERRAL_BONUS_DAYS} дней</b> ⭐\n\n"
                "Создаём ключ...",
                parse_mode="HTML",
            )
        except Exception:
            pass

        ok = await issue_subscription(
            bot=bot,
            tg_id=referrer_id,
            source="referral",
            days_override=REFERRAL_BONUS_DAYS,
            send_receipt=True,
        )

        if ok:
            logger.info(
                f"Реф. бонус [new key]: tg_id={referrer_id} +{REFERRAL_BONUS_DAYS}д ключ "
                f"за tg_id={referred_id}"
            )
        else:
            logger.error(
                f"Реф. бонус: не удалось создать ключ для referrer={referrer_id}"
            )
            try:
                await bot.send_message(
                    referrer_id,
                    "⚠️ Не удалось создать ключ автоматически. Обратитесь в поддержку.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
