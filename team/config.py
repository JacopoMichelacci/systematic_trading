"""
butter_chicken_pasta — team configuration.

Your bot IDs and capital allocation were set at registration.
The exchange grants each bot its allocated capital when it connects.

Environment variables:
    TEAM_ID         which bot this process is
    ARENA_TOKEN     your team's secret token (from registration — see .env)
    EXCHANGE_URL    full exchange URL (wss://… for a TLS-hosted arena;
                    set at registration — see .env)
    EXCHANGE_HOST   exchange hostname (default: localhost)
    EXCHANGE_PORT   exchange port     (default: 8765)
"""

import os

try:                       # credentials from `make register` (.env);
    from shared.envfile import load_env   # shell variables still win
    load_env()
except ImportError:        # standalone import without the engine on sys.path
    pass

EXCHANGE_URL = os.environ.get("EXCHANGE_URL") or (
    f"ws://{os.environ.get('EXCHANGE_HOST', 'localhost')}"
    f":{os.environ.get('EXCHANGE_PORT', '8765')}"
)
ARENA_TOKEN = os.environ.get("ARENA_TOKEN", "")

TEAM_NAME  = 'butter_chicken_pasta'
BROKER_IDS = ['butter_chicken_pasta_broker']
TRADER_IDS = ['butter_chicken_pasta_trader_1', 'butter_chicken_pasta_trader_2', 'butter_chicken_pasta_trader_3']

# Capital you allocated per bot (informational — the exchange enforces it):
CAPITAL = {'butter_chicken_pasta_broker': 250000, 'butter_chicken_pasta_trader_1': 250000, 'butter_chicken_pasta_trader_2': 250000, 'butter_chicken_pasta_trader_3': 250000}
