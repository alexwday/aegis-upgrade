"""Startup wiring checks for source pipeline modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connections.llm_connector import LLMClient
from connections.postgres_connector import check_connection
from .config_setup import (
    get_input_source_config,
    get_llm_auth_mode,
    get_llm_endpoint,
    get_llm_endpoint_mode,
    get_log_output_enabled,
    get_output_source_config,
    load_config,
)
from .logging_setup import get_stage_logger, setup_logging
from .ssl_setup import setup_ssl


@dataclass(frozen=True)
class StartupResult:
    """Summary of startup setup checks."""

    verify_ssl: bool
    log_output_enabled: bool
    input_source: str
    input_base_path: str
    output_source: str
    output_base_path: str
    postgres: dict[str, Any] | None
    llm: dict[str, str] | None


def run_startup(
    check_postgres: bool = True,
    check_llm: bool = True,
) -> StartupResult:
    """Run startup setup and optional live health checks.

    This loads local configuration, configures logging, applies SSL settings,
    and resolves the input/output source paths. The default check_postgres and
    check_llm values perform live PostgreSQL and LLM calls; pass False for
    deterministic setup validation that avoids external services.
    """
    load_config()
    log_output_enabled = get_log_output_enabled()
    setup_logging(enable_file=log_output_enabled)
    logger = get_stage_logger(__name__, "STARTUP")

    logger.info("Loading database configuration")
    logger.info("File log output enabled: %s", log_output_enabled)

    verify_ssl = setup_ssl()
    logger.info("SSL verification enabled: %s", verify_ssl)

    input_config = get_input_source_config()
    logger.info(
        "Input source configured: source=%s, base_path=%s",
        input_config.source,
        input_config.base_path,
    )
    output_config = get_output_source_config()
    logger.info(
        "Output source configured: source=%s, base_path=%s",
        output_config.source,
        output_config.base_path,
    )

    postgres_health = None
    if check_postgres:
        postgres_health = check_connection()
        logger.info(
            "Postgres health check passed: database=%s, user=%s, host=%s, "
            "port=%s, schema=%s",
            postgres_health["database"],
            postgres_health["user"],
            postgres_health["host"],
            postgres_health["port"],
            postgres_health["schema"],
        )

    llm_health = None
    if check_llm:
        llm_health = {
            "auth_mode": get_llm_auth_mode(),
            "endpoint_mode": get_llm_endpoint_mode(),
            "endpoint": get_llm_endpoint(),
        }
        llm_client = LLMClient(verify_ssl=verify_ssl)
        llm_client.test_connection()
        logger.info(
            "LLM health check passed: auth_mode=%s, endpoint_mode=%s, endpoint=%s",
            llm_health["auth_mode"],
            llm_health["endpoint_mode"],
            llm_health["endpoint"],
        )

    logger.info("Startup checks complete")
    return StartupResult(
        verify_ssl=verify_ssl,
        log_output_enabled=log_output_enabled,
        input_source=input_config.source,
        input_base_path=str(input_config.base_path),
        output_source=output_config.source,
        output_base_path=str(output_config.base_path),
        postgres=postgres_health,
        llm=llm_health,
    )
