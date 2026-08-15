"""Check catalogue.

Stages 0-2 are implemented. 3-7 are stubs in the roadmap, not silently missing —
`onboard` reports them as not-yet-implemented so nobody reads a clean run as
"cleared to trade".
"""

from __future__ import annotations

from ..core.check import Stage
from ..core.registry import Registry
from . import auth, environment, identity

IMPLEMENTED_STAGES = frozenset({Stage.ENVIRONMENT, Stage.IDENTITY, Stage.AUTH})

PENDING_STAGES: tuple[tuple[Stage, str], ...] = (
    (Stage.FUNDING, "collateral and allowances, and why /balance-allowance lies"),
    (Stage.MARKET_LIMITS, "tick grid, 5-token minimum, neg-risk rounding"),
    (Stage.ORDER_DRY_RUN, "build and sign a real order without posting it"),
    (Stage.WEBSOCKET, "subscribe, heartbeat, staleness, resequence on reconnect"),
    (Stage.RFQ, "combos-rfq-api quote submission and last look"),
)


def default_registry() -> Registry:
    registry = Registry()
    registry.add_all(environment.CHECKS)
    registry.add_all(identity.CHECKS)
    registry.add_all(auth.CHECKS)
    return registry


__all__ = ["default_registry", "IMPLEMENTED_STAGES", "PENDING_STAGES"]
