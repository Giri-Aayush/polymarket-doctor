"""Check catalogue. All eight stages are implemented."""

from __future__ import annotations

from ..core.check import Stage
from ..core.registry import Registry
from . import (
    auth,
    environment,
    funding,
    identity,
    market_limits,
    order_dryrun,
    rfq,
    websocket_feed,
)

IMPLEMENTED_STAGES = frozenset(Stage)

# Kept for the renderer's contract; empty now that every stage ships checks.
PENDING_STAGES: tuple[tuple[Stage, str], ...] = ()


def default_registry() -> Registry:
    registry = Registry()
    registry.add_all(environment.CHECKS)
    registry.add_all(identity.CHECKS)
    registry.add_all(auth.CHECKS)
    registry.add_all(funding.CHECKS)
    registry.add_all(market_limits.CHECKS)
    registry.add_all(order_dryrun.CHECKS)
    registry.add_all(websocket_feed.CHECKS)
    registry.add_all(rfq.CHECKS)
    return registry


__all__ = ["default_registry", "IMPLEMENTED_STAGES", "PENDING_STAGES"]
