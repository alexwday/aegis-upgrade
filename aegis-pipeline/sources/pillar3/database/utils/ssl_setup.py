"""SSL setup helpers for internal and public endpoints."""

from __future__ import annotations

import importlib
import logging

from .config_setup import get_ssl_verify, load_config

logger = logging.getLogger(__name__)


def setup_ssl() -> bool:
    """Apply SSL setup from env and return the requests verify flag.

    SSL_VERIFY=true means the app should verify TLS and attempt to enable
    RBC certificates through the optional rbc_security package. SSL_VERIFY=false
    skips RBC setup and returns False for connectors that accept a verify flag.
    """
    load_config()
    verify_ssl = get_ssl_verify()
    if not verify_ssl:
        return False

    try:
        module = importlib.import_module("rbc_security")
    except ImportError:
        logger.info("rbc_security not available, using system certificates")
        return True

    module.enable_certs()
    logger.info("RBC SSL certificates enabled")
    return True
