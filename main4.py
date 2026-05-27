"""
AstroVPN Bot v2.0 — Multi-server architecture

Точка входа: инициализация БД, бота, scheduler'а, webhook-сервера для Tribute
и подключение роутеров из handlers/.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, PORT
from database import init_db
from notifications import check_expired_subscriptions, reminders_job

from handlers.admin import router as admin_router
from handlers.servers import router as servers_router
from handlers.payments import router as payments_router, tribute_webhook_handler
from handlers.user import router as user_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_dp() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # Порядок важен: admin/servers/payments — ДО user, потому что user содержит
    # fallback-хэндлер @router.message() который ловит всё подряд.
    dp.include_router(admin_router)
    dp.include_router(servers_router)
    dp.include_router(payments_router)
    dp.include_router(user_router)
    return dp


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Проверьте переменные окружения.")

    await init_db()
    logger.info("База данных инициализирована")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dp()

    # ── Scheduler ────────────────────────────────────
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_expired_subscriptions,
        trigger="interval",
        minutes=10,
        args=[bot],
        id="check_subs",
        replace_existing=True,
    )
    scheduler.add_job(
        reminders_job,
        trigger="interval",
        hours=1,
        args=[bot],
        id="reminders",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler запущен: check_expired (10 мин), reminders (1 час)")

    # ── Tribute webhook + health ─────────────────────
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

    # ── Polling ──────────────────────────────────────
    logger.info("Запуск бота (polling)...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
