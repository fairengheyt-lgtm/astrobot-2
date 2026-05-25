# 🌐 AstroVPN Bot

Telegram VPN-бот. VLESS + Reality + автоматическая выдача ключей через 3X-UI.

## 📁 Файлы

| Файл | Описание |
|------|----------|
| `main.py` | Весь бот (один файл) |
| `requirements.txt` | Зависимости |
| `railway.toml` | Конфиг Railway |
| `.env.example` | Пример переменных окружения |

## ⚙️ Переменные окружения Railway

Зайди в Railway → твой сервис → **Variables** и добавь:

```
BOT_TOKEN=токен_от_BotFather
ADMIN_IDS=твой_telegram_id
XUI_HOST=http://89.127.207.207
XUI_PORT=2053
XUI_USERNAME=admin
XUI_PASSWORD=пароль_от_панели
XUI_INBOUND_ID=1
VPN_SERVER_IP=89.127.207.207
VPN_PORT=443
VPN_PUBLIC_KEY=публичный_ключ_из_панели
VPN_SHORT_ID=short_id_из_панели
VPN_SNI=www.apple.com
VPN_FINGERPRINT=chrome
VPN_FLOW=xtls-rprx-vision
STARS_AMOUNT=199
PRICE_RUB=199
TRIBUTE_LINK=https://t.me/tribute/app?startapp=dI5p
SUPPORT_USERNAME=@твой_ник
SUBSCRIPTION_DAYS=30
PORT=8080
```

## 🔑 Где взять VPN_PUBLIC_KEY и VPN_SHORT_ID

1. Открой 3X-UI панель → Inbounds
2. Нажми на твой VLESS+Reality inbound → Edit
3. Скопируй **Public Key** и **Short ID**

## 🌐 Tribute Webhook

1. В Railway → Settings → **Networking** → expose port **8080**
2. Скопируй публичный URL (например `https://xxx.up.railway.app`)
3. В Tribute Dashboard укажи webhook URL:
   ```
   https://xxx.up.railway.app/tribute/webhook
   ```

## 👑 Команды администратора

```
/admin    — справка
/users    — список пользователей
/keys     — активные ключи
/give ID  — выдать подписку
/revoke ID — отозвать подписку
```

## 💡 Что изменено по сравнению с предыдущей версией

- ✅ SQLite вместо JSON файлов (надёжно на Railway)
- ✅ Токен и все секреты из ENV (не в коде!)
- ✅ 3X-UI интеграция — ключи создаются автоматически, не нужно добавлять вручную
- ✅ Tribute: читает `payload.telegram_user_id`, НЕ comment
- ✅ Stars: `provider_token=""`, currency `XTR`
- ✅ Все `edit_message_text` в try/except (нет ошибки "message not modified")
- ✅ parse_mode="HTML" везде (не Markdown)
- ✅ Scheduler: автоотключение истёкших подписок каждые 10 минут
- ✅ Продление: если есть активная подписка — добавляет дни к текущей дате истечения
