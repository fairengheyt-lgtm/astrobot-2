"""
config.py — все ENV переменные и константы AstroVPN.

ENV переменные сохранены для обратной совместимости и используются как
fallback для первого сервера при миграции на мультисерверную архитектуру.
"""

import os

# ── Telegram ──────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW    = os.getenv("ADMIN_IDS", "")
ADMIN_IDS        = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

# ── 3X-UI (fallback для первого сервера) ──────────────
XUI_HOST         = os.getenv("XUI_HOST", "http://89.127.207.207")
XUI_PORT         = int(os.getenv("XUI_PORT", "2053"))
XUI_USERNAME     = os.getenv("XUI_USERNAME", "admin")
XUI_PASSWORD     = os.getenv("XUI_PASSWORD", "")
XUI_INBOUND_ID   = int(os.getenv("XUI_INBOUND_ID", "1"))
XUI_BASE_PATH    = os.getenv("XUI_BASE_PATH", "")
XUI_TOKEN        = os.getenv("XUI_TOKEN", "")

# ── VPN params для VLESS-ссылки (fallback) ────────────
VPN_SERVER_IP    = os.getenv("VPN_SERVER_IP", "89.127.207.207")
VPN_PORT         = int(os.getenv("VPN_PORT", "443"))
VPN_PUBLIC_KEY   = os.getenv("VPN_PUBLIC_KEY", "")
VPN_SHORT_ID     = os.getenv("VPN_SHORT_ID", "")
VPN_SNI          = os.getenv("VPN_SNI", "www.apple.com")
VPN_FINGERPRINT  = os.getenv("VPN_FINGERPRINT", "chrome")
VPN_FLOW         = os.getenv("VPN_FLOW", "xtls-rprx-vision")

# ── Оплата ────────────────────────────────────────────
STARS_AMOUNT     = int(os.getenv("STARS_AMOUNT", "199"))
PRICE_RUB        = int(os.getenv("PRICE_RUB", "199"))
TRIBUTE_LINK     = os.getenv("TRIBUTE_LINK", "https://t.me/tribute/app?startapp=dI5p")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@astrovpn_support")

# ── Подписка ──────────────────────────────────────────
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))

# ── Реферальная программа ─────────────────────────────
REFERRAL_BONUS_DAYS = 7

# ── Мультисервер ──────────────────────────────────────
DEFAULT_SERVER_MAX_CLIENTS = 35  # лимит клиентов на сервер по умолчанию

# ── БД ────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "astrovpn.db")

# ── Web ───────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))

# ── Рассылки ──────────────────────────────────────────
BROADCAST_DELAY_SECONDS = 0.05  # 50ms между сообщениями ≈ 20 msg/s
