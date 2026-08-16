"""Stage 7 — can the partner reach the RFQ gateway, and do they know its shape?

The RFQ gateway is a separate service on combos-rfq-api.polymarket.com, not a
route on the CLOB. clob.polymarket.com/rfq 404s through nginx, which is where
everyone looks first and where an afternoon goes. Verified live 2026-08-15:

    GET https://combos-rfq-api.polymarket.com/v1/rfq/combo-markets -> 200
    {"markets": [{"id": ..., "condition_id": "0x...", "position_ids": [...],
                  "pending": bool, ...}]}

The maker side, per Polymarket's published OpenAPI spec
(docs.polymarket.com/api-spec/combos-rfq-openapi.yaml, fetched 2026-08-15), is
three POSTs — /v1/maker/quotes, /v1/maker/quotes/cancel,
/v1/maker/confirmations — each declaring the five POLY_* CLOB L2 headers
(POLY_API_KEY / POLY_ADDRESS / POLY_SIGNATURE / POLY_PASSPHRASE /
POLY_TIMESTAMP) as its security scheme. This stage never calls them: a signed
POST to /v1/maker/quotes places a real quote, and the unauthenticated behaviour
is already known — it answers {"error": "invalid l2 address header"}, an error
vocabulary the CLOB never uses. Both facts are encoded here as knowledge for
the finding text instead of being re-triggered on every run.
"""

from __future__ import annotations

from ..core.check import Check, Finding, Stage
from ..core.context import Context
from ..core.facts import Fact

COMBO_MARKETS_PATH = "/v1/rfq/combo-markets"

# Documented but deliberately never called: all three mutate maker state, and
# the first one submits a live quote if the credentials are real.
MAKER_QUOTE_PATH = "/v1/maker/quotes"
MAKER_CANCEL_PATH = "/v1/maker/quotes/cancel"
MAKER_CONFIRM_PATH = "/v1/maker/confirmations"

# Observed live on an unauthenticated POST to /v1/maker/quotes (2026-08-15).
# Worth quoting verbatim: partners grepping the CLOB error tables for this
# string find nothing, because it belongs to the RFQ service's own vocabulary.
RFQ_AUTH_ERROR = "invalid l2 address header"


class RfqGateway(Check):
    """One read-only GET that also front-loads what the maker flow looks like.

    The passing detail carries the three things partners otherwise learn the
    hard way: the separate host, the quote -> cancel -> last-look shape, and
    the service's distinct error strings.
    """

    id = "rfq.gateway"
    stage = Stage.RFQ
    title = "RFQ gateway is reachable"
    reads = frozenset({Fact.HAS_L2_CREDENTIALS})
    writes = frozenset({Fact.RFQ_REACHABLE})

    def run(self, ctx: Context) -> Finding:
        url = ctx.endpoints.rfq_url(COMBO_MARKETS_PATH)
        response = ctx.probe.get(url)

        remedy = (
            f"RFQ lives on its own host, {ctx.endpoints.rfq} — not under the "
            f"CLOB. {ctx.endpoints.clob}/rfq 404s through nginx, so if you "
            f"derived the RFQ base URL from the CLOB host, that's the bug."
        )

        if not response.reached_server:
            ctx.facts.set(Fact.RFQ_REACHABLE, False)
            return Finding.fail(
                f"cannot reach {ctx.endpoints.rfq}",
                detail=response.error,
                remedy=remedy,
            )

        if not response.ok:
            ctx.facts.set(Fact.RFQ_REACHABLE, False)
            return Finding.fail(
                f"{ctx.endpoints.rfq} returned {response.status}",
                detail=response.error_message()
                or f"unexpected status from {COMBO_MARKETS_PATH}",
                remedy=remedy,
                status=response.status,
            )

        markets = response.body.get("markets") if isinstance(response.body, dict) else None
        if not isinstance(markets, list):
            # A 200 that isn't the documented shape usually means a proxy or
            # captive portal answered instead of the gateway. Not reachable in
            # any sense a maker integration can use.
            ctx.facts.set(Fact.RFQ_REACHABLE, False)
            return Finding.fail(
                "combo-markets answered 200 without a markets list",
                detail=f"body was {response.body!r}",
                remedy=remedy,
            )

        ctx.facts.set(Fact.RFQ_REACHABLE, True)

        detail = (
            f"RFQ is its own service — {ctx.endpoints.clob}/rfq 404s through "
            f"nginx, so this host has to be configured directly, not derived "
            f"from the CLOB. The maker flow is quote → cancel → last-look "
            f"confirm: POST {MAKER_QUOTE_PATH}, POST {MAKER_CANCEL_PATH}, "
            f"POST {MAKER_CONFIRM_PATH}, all under the five POLY_* L2 headers "
            f"per the published OpenAPI spec. Its error strings are its own "
            f"too, and it has more than one auth-failure mode: a quote POST "
            f'with no address header answers {{"error": "{RFQ_AUTH_ERROR}"}}, '
            f"while one with a present-but-rejected HMAC answers the gRPC "
            f'{{"error": "rpc error: code = PermissionDenied desc = could not '
            f'validate hmac signature"}} (both verified live). Neither appears '
            f"in any CLOB error table — when they show up in your logs, look "
            f"here, not at the CLOB."
        )

        if not ctx.facts.get(Fact.HAS_L2_CREDENTIALS):
            detail += (
                " Quote submission itself was not exercised: it needs L2 "
                "credentials and would place a real quote, which is out of "
                "scope for a diagnostic by design."
            )

        return Finding.ok(
            f"{ctx.endpoints.rfq} responding, {len(markets)} combo markets listed",
            detail=detail,
            market_count=len(markets),
            latency_ms=response.elapsed_ms,
        )


CHECKS = (RfqGateway(),)
