"""
butter_chicken_pasta — broker (market maker).

Posts two-sided quotes and earns the spread plus maker rebates on every
passive fill. Three decisions, three methods — override any of them.
Inventory management (skew) is the key to staying solvent: the exchange
charges margin interest and liquidates teams below maintenance.

Run:
    TEAM_ID=butter_chicken_pasta_broker python -m team.broker
"""

from __future__ import annotations

from arena import Broker


class MyBroker(Broker):
    """Your market-making logic. An empty subclass already quotes."""

    def spread(self, symbol, price, history):
        if (len(history) < 25):
            return None
        moves = [abs(b-a) for a, b in zip(history, history[1:])]
        vol = sum(moves[-20:]) / 20
        return max(0.05, vol * 4)

    def skew(self, symbol, inventory):
        return -inventory * 0.02

    # Optional extra hooks:
    #   def toxic(self, trader_id): ...                 # Level 5
    #   def on_fill(self, side, symbol, quantity, price): ...


if __name__ == "__main__":
    MyBroker().run()
