"""
SSL configuration module.

This module handles SSL certificate loading based on environment configuration.
"""

import os
from typing import Dict, Optional, Union

from .logging import get_logger
from .settings import config


def setup_ssl() -> Dict[str, Union[bool, Optional[str]]]:
    """
    Setup SSL configuration based on environment variables.

    Checks SSL_VERIFY and SSL_CERT_PATH environment variables and returns
    a consistent output schema for both verify and non-verify scenarios.

    Returns:
        Dictionary with SSL configuration:
        - "success": bool - Whether SSL setup succeeded
        - "verify": bool - Whether to verify SSL (only if success=True)
        - "cert_path": str or None - Path to certificate file if verify is True
        - "status": str - Operation status ("Success" or "Failure")
        - "error": str or None - Error message if setup failed
        - "decision_details": str - Human-readable description of the outcome

        # Returns: {"success": True, "verify": False, "cert_path": None,
        #          "status": "disabled", "error": None,
        #          "decision_details": "SSL verification: disabled"}
        # Returns: {"success": False, "verify": False, "cert_path": None,
        #          "status": "failed", "error": "Certificate not found",
        #          "decision_details": "SSL setup failed: Certificate not found"}
    """
    logger = get_logger()

    try:
        # Check if SSL verification is enabled
        if not config.ssl_verify:
            logger.debug("SSL verification disabled")
            return {
                "success": True,
                "verify": False,
                "cert_path": None,
                "status": "Success",
                "error": None,
                "decision_details": "SSL verification: disabled",
            }

        # SSL verification is enabled
        cert_path = config.ssl_cert_path

        if cert_path:
            # Expand user path if needed
            cert_path = os.path.expanduser(cert_path)

            # Check if certificate file exists
            if not os.path.exists(cert_path):
                error_msg = f"SSL certificate file not found: {cert_path}"
                logger.error(error_msg)
                return {
                    "success": False,
                    "verify": False,
                    "cert_path": None,
                    "status": "Failure",
                    "error": error_msg,
                    "decision_details": f"SSL setup failed: {error_msg}",
                }

            logger.info("SSL verification enabled with certificate", cert_path=cert_path)
            return {
                "success": True,
                "verify": True,
                "cert_path": cert_path,
                "status": "Success",
                "error": None,
                "decision_details": "SSL verification: enabled with certificate",
            }

        logger.info("SSL verification enabled with system certificates")
        return {
            "success": True,
            "verify": True,
            "cert_path": None,
            "status": "enabled",
            "error": None,
            "decision_details": "SSL verification: enabled with system certificates",
        }

    except Exception as e:  # pylint: disable=broad-exception-caught
        # SSL setup must not crash the application; returns safe defaults on any error.
        error_msg = f"Unexpected error during SSL setup: {str(e)}"
        logger.error(error_msg)
        return {
            "success": False,
            "verify": False,
            "cert_path": None,
            "status": "Failure",
            "error": error_msg,
            "decision_details": f"SSL setup failed: {str(e)}",
        }
