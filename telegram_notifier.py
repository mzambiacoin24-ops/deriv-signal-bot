import asyncio
import logging

import requests


log = logging.getLogger("telegram-notifier")


class TelegramNotifier:
    """Telegram sender used by signal_bot.py."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()

        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN haijawekwa.")

        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID haijawekwa.")

        self.url = (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        )

    def _send_sync(self, text: str):
        response = requests.post(
            self.url,
            data={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )

        if not response.ok:
            raise RuntimeError(
                f"Telegram API error {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not data.get("ok"):
            raise RuntimeError(
                f"Telegram API haijakubali ujumbe: {data}"
            )

        return data

    async def send(self, text: str):
        """Tuma ujumbe Telegram bila kuzuia event loop."""
        try:
            return await asyncio.to_thread(
                self._send_sync,
                text,
            )
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
            raise
