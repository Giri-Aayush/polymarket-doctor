"""Preflight diagnostics for Polymarket integrations."""

from .api import run_onboard, to_json
from .core.context import Credentials

__version__ = "0.1.0"

__all__ = ["run_onboard", "to_json", "Credentials", "__version__"]
