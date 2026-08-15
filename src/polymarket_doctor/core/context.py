"""Per-run state: what the user told us, plus what we've learned so far."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..net.chain import ChainReader
from ..net.endpoints import Endpoints
from ..net.http import HttpProbe
from .facts import FactStore


@dataclass(slots=True)
class Credentials:
    """L2 API credentials.

    Never rendered. `redacted()` is what goes anywhere near a report — the whole
    point of this tool is that a partner can paste its output into a support
    thread without leaking anything.
    """

    api_key: str
    secret: str
    passphrase: str

    def redacted(self) -> dict[str, str]:
        return {
            "api_key": _mask(self.api_key),
            "secret": "<redacted>",
            "passphrase": "<redacted>",
        }


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}…{value[-4:]}"


@dataclass(slots=True)
class Context:
    """Everything a check is allowed to touch.

    Checks get this and nothing else — no module-level clients, no ambient
    config. Makes them trivial to test against a stub probe.
    """

    endpoints: Endpoints
    probe: HttpProbe
    chain: ChainReader | None = None
    facts: FactStore = field(default_factory=FactStore)

    signer_address: str | None = None
    funder_address: str | None = None
    credentials: Credentials | None = None

    # Off by default. Every check that would place, cancel, or mutate anything
    # has to gate on this, and nothing in stages 0-2 ever does.
    allow_live_orders: bool = False

    @property
    def has_credentials(self) -> bool:
        return self.credentials is not None
