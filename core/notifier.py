"""通知(F-7)。Slack Incoming Webhook に送信する。

- 対象: 取引・エラー・ガードレール発動・緊急停止/再開・プロセス起動停止・フィード断
- Webhook 未設定時はログ出力のみ(開発時)
- LINE 対応は将来追加(send() の呼び出し側は通知先を知らない構造)
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, slack_webhook_url: str = "") -> None:
        self._url = slack_webhook_url

    async def send(self, text: str, *, level: str = "info") -> None:
        prefix = {"info": "ℹ️", "trade": "💹", "warn": "⚠️", "error": "🚨"}.get(level, "")
        message = f"{prefix} {text}".strip()
        logger.info("notify[%s]: %s", level, text)
        if not self._url:
            return
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(self._url, json={"text": message}, timeout=aiohttp.ClientTimeout(10)),
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as e:
            # 通知失敗で本体を止めない(ログには残す)
            logger.error("Slack 通知に失敗: %s", e)
