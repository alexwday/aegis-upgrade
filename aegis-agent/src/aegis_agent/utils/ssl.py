"""SSL configuration module."""

import importlib
from typing import Dict, Union

from .logging import get_logger
from .settings import config


def setup_ssl() -> Dict[str, Union[bool, str, None]]:
    """
    Setup SSL configuration based on environment variables.

    Checks SSL_VERIFY and returns a consistent output schema for both verify and
    non-verify scenarios. When SSL verification is enabled, RBC certificates
    must be enabled through rbc_security; no certificate-file or system-cert
    fallback is supported.

    Returns:
        Dictionary with SSL configuration:
        - "success": bool - Whether SSL setup succeeded
        - "verify": bool - Whether to verify SSL (only if success=True)
        - "status": str - Operation status ("Success" or "Failure")
        - "error": str or None - Error message if setup failed
        - "decision_details": str - Human-readable description of the outcome

        # Returns: {"success": True, "verify": False,
        #          "status": "disabled", "error": None,
        #          "decision_details": "SSL verification: disabled"}
        # Returns: {"success": False, "verify": True,
        #          "status": "Failure", "error": "rbc_security not available",
        #          "decision_details": "SSL setup failed: rbc_security not available"}
    """
    logger = get_logger()

    try:
        # Check if SSL verification is enabled
        if not config.ssl_verify:
            logger.debug("SSL verification disabled")
            return {
                "success": True,
                "verify": False,
                "status": "Success",
                "error": None,
                "decision_details": "SSL verification: disabled",
            }

        try:
            rbc_security = importlib.import_module("rbc_security")
        except ImportError as exc:
            error_msg = (
                "SSL_VERIFY=true requires the rbc_security package; "
                "no SSL fallback is configured."
            )
            logger.error(error_msg)
            return {
                "success": False,
                "verify": True,
                "status": "Failure",
                "error": error_msg,
                "decision_details": f"SSL setup failed: {error_msg}",
            }

        try:
            rbc_security.enable_certs()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error_msg = f"Failed to enable RBC SSL certificates: {str(exc)}"
            logger.error(error_msg)
            return {
                "success": False,
                "verify": True,
                "status": "Failure",
                "error": error_msg,
                "decision_details": f"SSL setup failed: {error_msg}",
            }

        logger.info("SSL verification enabled with rbc_security")
        return {
            "success": True,
            "verify": True,
            "status": "Success",
            "error": None,
            "decision_details": "SSL verification: enabled with rbc_security",
        }

    except Exception as e:  # pylint: disable=broad-exception-caught
        # SSL setup must not crash the application; returns safe defaults on any error.
        error_msg = f"Unexpected error during SSL setup: {str(e)}"
        logger.error(error_msg)
        return {
            "success": False,
            "verify": True,
            "status": "Failure",
            "error": error_msg,
            "decision_details": f"SSL setup failed: {str(e)}",
        }
