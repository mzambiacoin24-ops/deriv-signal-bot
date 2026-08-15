import asyncio
import logging

import requests


log = logging.getLogger("telegram-notifier")


class TelegramNotifier:
    """Telegram sender used by signal_bot.py.

    Supports one or multiple Telegram Chat IDs.
    Example:
        TELEGRAM_CHAT_ID=123456789,987654321
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = (bot_token or "").strip()

        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN haijawekwa.")

        self.chat_ids = self._parse_chat_ids(chat_id)

        if not self.chat_ids:
            raise ValueError("TELEGRAM_CHAT_ID haijawekwa.")

        self.url = (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        )

    @staticmethod
    def _parse_chat_ids(chat_id: str):
        """Convert one or many comma-separated Chat IDs into a clean list."""
        raw_ids = str(chat_id or "").replace("\n", ",").split(",")

        result = []
        seen = set()

        for item in raw_ids:
            value = item.strip()

            if not value:
                continue

            # Telegram group/channel IDs can be negative.
            try:
                int(value)
            except ValueError:
                raise ValueError(
                    f"Telegram Chat ID si sahihi: {value}"
                )

            if value not in seen:
                seen.add(value)
                result.append(value)

        return result

    def _send_one_sync(self, text: str, chat_id: str):
        response = requests.post(
            self.url,
            data={
                "chat_id": chat_id,
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
                f"Telegram API haijakubali ujumbe kwa "
                f"Chat ID {chat_id}: {data}"
            )

        return data

    def _send_sync(self, text: str):
        """Tuma ujumbe kwa kila Chat ID."""
        results = []

        for chat_id in self.chat_ids:
            results.append(
                self._send_one_sync(
                    text,
                    chat_id,
                )
            )

        return results

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

    def get_chat_ids(self):
        """Return configured Chat IDs."""
        return list(self.chat_ids)
