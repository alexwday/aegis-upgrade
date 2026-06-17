"""SSL setup helpers for internal and public endpoints."""

from __future__ import annotations

import importlib
import logging

from .config_setup import get_ssl_verify, load_config

logger = logging.getLogger(__name__)


def enable_rbc_certs() -> None:
    """Enable RBC certificates or fail when rbc_security is unavailable."""
    try:
        module = importlib.import_module("rbc_security")
    except ImportError as exc:
        raise RuntimeError(
            "SSL_VERIFY=true requires the rbc_security package; no SSL fallback is configured."
        ) from exc

    module.enable_certs()
    logger.info("RBC SSL certificates enabled")


def setup_ssl() -> bool:
    """Apply SSL setup from env and return the requests verify flag.

    SSL_VERIFY=true means the app should verify TLS using RBC certificates
    enabled through the required rbc_security package. SSL_VERIFY=false skips
    RBC setup and returns False for connectors that accept a verify flag.
    """
    load_config()
    verify_ssl = get_ssl_verify()
    if not verify_ssl:
        return False

    enable_rbc_certs()
    return True
