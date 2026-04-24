"""Lightweight Telegram notifier. Fire-and-forget; never blocks trading."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

log = logging.getLogger("polybot.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if not self.enabled:
            log.info("telegram disabled")
            return
        self._task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            pass

    async def _drain(self) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    msg = await self._queue.get()
                    payload = {"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"}
                    try:
                        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                            if r.status != 200:
                                log.warning("telegram status %s", r.status)
                    except Exception as e:  # noqa: BLE001
                        log.warning("telegram post failed: %s", e)
                except asyncio.CancelledError:
                    return
