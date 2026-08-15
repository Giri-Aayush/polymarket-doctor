"""Where Polymarket's services actually live.

Hosts verified against production on 2026-08-15. Two worth calling out:

- RFQ is its own service on combos-rfq-api.polymarket.com. Requests to
  clob.polymarket.com/rfq 404 through nginx, which reads like a routing bug and
  costs people an afternoon.
- clob-v2.polymarket.com appears in rs-clob-client-v2's README. It resolves
  (everything under polymarket.com resolves to the same edge) but the CLOB is
  served from clob.polymarket.com, which already answers /version with 2.
"""

from __future__ import annotations

from dataclasses import dataclass

CLOB = "https://clob.polymarket.com"
RFQ = "https://combos-rfq-api.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"
DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
WEBSOCKET = "wss://ws-subscriptions-clob.polymarket.com/ws"

# Named in a shipped README but not the service address. Flagged, not used.
DEPRECATED_HOSTS = {
    "https://clob-v2.polymarket.com": (
        "rs-clob-client-v2's README points here; the CLOB answers on "
        "clob.polymarket.com and reports protocol 2 already"
    ),
}


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Service addresses for one run. Overridable so tests never touch prod."""

    clob: str = CLOB
    rfq: str = RFQ
    relayer: str = RELAYER
    data: str = DATA
    gamma: str = GAMMA
    websocket: str = WEBSOCKET

    def clob_url(self, path: str) -> str:
        return f"{self.clob}/{path.lstrip('/')}"

    def relayer_url(self, path: str) -> str:
        return f"{self.relayer}/{path.lstrip('/')}"

    def rfq_url(self, path: str) -> str:
        return f"{self.rfq}/{path.lstrip('/')}"

    def data_url(self, path: str) -> str:
        return f"{self.data}/{path.lstrip('/')}"
