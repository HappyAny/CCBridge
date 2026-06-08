BOT_TOKEN = "telegram-bot-token"

# Optional: keep empty when you use ALLOWED_TELEGRAM_CHAT_IDS.
TELEGRAM_CHAT_ID = None

# Required. Only these Telegram chats can control the bridge.
ALLOWED_TELEGRAM_CHAT_IDS = [
    123456789,
]

# Optional HTTP control API settings.
HTTP_CONTROL_HOST = "127.0.0.1"
HTTP_CONTROL_PORT = 8765
HTTP_CONTROL_TOKEN = ""
