"""L2 request signing, reimplemented to match the SDK byte for byte.

We re-implement rather than import so the tool can diagnose an installed SDK
without depending on it, and so it still works when none is installed.

Cross-checked against py-clob-client-v2 (signing/hmac.py, headers/headers.py) on
2026-08-15. Two details in there that cost people days:

- POLY_TIMESTAMP is unix *seconds* — `int(datetime.now().timestamp())`. The
  timestamp inside an order struct is milliseconds, and so is the one on /book.
  Mixed units across one API, no error when you pick wrong, just a 401 later.
- The body is folded in as `str(body).replace("'", '"')`, i.e. Python's repr with
  quotes swapped, not `json.dumps`. For a plain string-to-string dict those
  agree. The moment a bool or None appears they don't, because repr writes
  True/None where JSON wants true/null, and the server computes a different
  digest. That's py-clob-client-v2#108.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

CLOB_AUTH_DOMAIN_NAME = "ClobAuthDomain"
CLOB_AUTH_DOMAIN_VERSION = "1"
CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

HEADER_ADDRESS = "POLY_ADDRESS"
HEADER_SIGNATURE = "POLY_SIGNATURE"
HEADER_TIMESTAMP = "POLY_TIMESTAMP"
HEADER_NONCE = "POLY_NONCE"
HEADER_API_KEY = "POLY_API_KEY"
HEADER_PASSPHRASE = "POLY_PASSPHRASE"


class SecretFormatError(ValueError):
    """The API secret isn't url-safe base64, so it can't key the HMAC."""


def canonical_body(body: Any) -> str:
    """Reproduce the SDK's body serialization, warts included."""
    if body is None:
        return ""
    return str(body).replace("'", '"')


def body_is_hmac_safe(body: Any) -> bool:
    """True when repr-with-swapped-quotes happens to equal valid JSON.

    Anything containing a bool, None, or a non-ASCII/quote-bearing string drifts
    from JSON and produces a digest the server won't reproduce.
    """
    rendered = canonical_body(body)
    if not rendered:
        return True
    try:
        return json.loads(rendered) == body
    except (json.JSONDecodeError, TypeError):
        return False


def build_hmac_signature(secret: str, timestamp: int | str, method: str,
                         request_path: str, body: Any = None) -> str:
    """HMAC-SHA256 over timestamp + method + path + body, url-safe base64."""
    try:
        key = base64.urlsafe_b64decode(secret)
    except (base64.binascii.Error, ValueError) as exc:  # type: ignore[attr-defined]
        raise SecretFormatError(
            "API secret is not url-safe base64. Copying it out of a JSON blob "
            "and losing the trailing '=' padding is the usual cause."
        ) from exc

    message = f"{timestamp}{method}{request_path}"
    if body:
        message += canonical_body(body)

    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def build_l2_headers(address: str, api_key: str, secret: str, passphrase: str,
                     timestamp: int, method: str, request_path: str,
                     body: Any = None) -> Mapping[str, str]:
    """Full L2 header set.

    POLY_ADDRESS is the wallet the key is bound to, never the api_key — putting
    the key there is the most common way to earn a 401 that says nothing useful.
    """
    return {
        HEADER_ADDRESS: address,
        HEADER_API_KEY: api_key,
        HEADER_PASSPHRASE: passphrase,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_SIGNATURE: build_hmac_signature(secret, timestamp, method, request_path, body),
    }
