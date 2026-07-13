"""エントリポイント: `python -m core.main`。

例:
    # 実データ + Claude 実判断(要 ANTHROPIC_API_KEY)
    python -m core.main

    # 開発・検証: シミュレーションフィード + モック判断(API 不要)
    python -m core.main --feed sim --llm mock --cycle 15
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

from aiohttp import web

from core.agent import AnthropicTraderAgent, MockTraderAgent, TraderAgent
from core.config import Settings
from core.engine import Engine
from core.feed import BitflyerFeed, PriceFeed, SimFeed
from core.notifier import Notifier
from core.server import build_app
from core.store import Store

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="investment_agent core (Phase 1: paper trade)")
    p.add_argument("--feed", choices=["bitflyer", "sim"], default=None,
                   help="価格フィード(既定: 設定値=bitflyer)")
    p.add_argument("--llm", choices=["anthropic", "mock"], default=None,
                   help="判断エージェント(既定: 設定値=anthropic)")
    p.add_argument("--cycle", type=float, default=None, help="判断サイクル秒(既定: 300)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--db", type=str, default=None)
    return p.parse_args()


async def run() -> None:
    args = parse_args()
    settings = Settings()
    if args.feed:
        settings.feed = args.feed
    if args.llm:
        settings.llm = args.llm
    if args.cycle:
        settings.decision_interval_sec = args.cycle
    if args.port:
        settings.port = args.port
    if args.db:
        settings.db_path = args.db
    if settings.mode != "paper":
        # LIVE(Phase 2)は発注者の明示的承認なしに有効化しない(安全ルール 4)
        raise SystemExit("Phase 1 では mode=paper のみサポートします")

    store = Store(settings.db_path)
    await store.open()
    feed: PriceFeed = SimFeed() if settings.feed == "sim" else BitflyerFeed()
    agent: TraderAgent = (
        MockTraderAgent() if settings.llm == "mock" else AnthropicTraderAgent(settings, store)
    )
    notifier = Notifier(settings.slack_webhook_url)
    engine = Engine(settings, store, feed, agent, notifier)

    app = build_app(engine, settings.auth_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("dashboard server: http://%s:%d", settings.host, settings.port)

    await engine.start()
    try:
        await asyncio.Event().wait()  # 常駐(Ctrl-C / SIGTERM で終了)
    finally:
        await engine.stop()
        await runner.cleanup()
        await store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
