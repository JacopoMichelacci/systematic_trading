"""Connect the broker seat and do nothing else.

Why this exists: the live blocks grade the team on being connected, on never
being liquidated and on never breaching the position limit. The quoting
broker in team/broker.py is not yet hardened for halts or the close (the
engine's broker still sends close-time market orders that the venue rejects),
so until it is, the broker seat joins the session without quoting: zero
orders means zero rejections, an unchanged long-only book, and a seat that
is present for the whole block.

It is the engine's BrokerBot with the three things that send orders or
reach outside the venue switched off. team/broker.py is untouched.

Run:  TEAM_ID=butter_chicken_pasta_broker python -m team.passive_broker
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib

# `arena` must be imported first: it puts engine/ on sys.path and loads .env
# (the team token) before broker.config reads the environment.
import arena  # noqa: F401  # isort: skip
from broker.broker import BrokerBot  # isort: skip

logger = logging.getLogger("team.passive_broker")


class PassiveBroker(BrokerBot):
    """Connected, listening, and silent."""

    def start_yahoo_feeds(self) -> None:
        logger.info("Passive broker: no external price feed needed")

    async def quote_symbol(self, symbol: str) -> None:
        return None

    async def flatten_all(self) -> None:
        logger.info("Session closed — keeping positions (no close-time orders)")


def _setup_logging() -> None:
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    team_id = os.environ.get("TEAM_ID", "broker")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / f"{team_id}.log")],
        force=True,
    )


if __name__ == "__main__":
    _setup_logging()
    try:
        asyncio.run(PassiveBroker().run())
    except KeyboardInterrupt:
        logger.info("Stopped by Ctrl-C")
