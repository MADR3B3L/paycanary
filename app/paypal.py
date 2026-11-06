"""Compatibility re-export: the PayPal client now lives inside its sealed provider folder (app/providers/paypal/)."""
from .providers.paypal.client import *  # noqa: F401,F403
from .providers.paypal.client import SANDBOX_BASE, PayPalClient, PayPalError, redact  # noqa: F401
