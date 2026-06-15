"""
Database filtering utilities.

Handles filtering of available databases based on user-provided database names.
"""

from typing import Dict, List, Optional

import yaml

from .logging import get_logger


def get_available_databases() -> Dict[str, Dict]:
    """
    Load all available databases from the database.yaml file.

    Returns:
        Dictionary of database configurations keyed by database ID
    """
    logger = get_logger()

    try:
        # Load database YAML
        import os

        yaml_path = os.path.join(
            os.path.dirname(__file__), "..", "model", "prompts", "global", "database.yaml"
        )

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Convert list to dict keyed by ID
        databases = {}
        for db in data.get("databases", []):
            databases[db["id"]] = db

        logger.debug("Loaded databases", count=len(databases))
        return databases

    except Exception as e:
        logger.error("Failed to load databases", error=str(e))
        return {}


def filter_databases(db_names: Optional[List[str]] = None) -> Dict[str, Dict]:
    """
    Filter available databases based on provided database names.

    Args:
        db_names: List of database IDs to include. If None, returns all databases.

    Returns:
        Filtered dictionary of database configurations
    """
    logger = get_logger()

    # Log what we received
    logger.info(
        "filter_databases.called",
        db_names=db_names,
        db_names_count=len(db_names) if db_names else 0,
        db_names_type=type(db_names).__name__,
        is_none=db_names is None,
        is_empty=not db_names if db_names is not None else None,
    )

    # Load all databases
    all_databases = get_available_databases()

    # If no filter, return all
    if not db_names:
        logger.info(
            "No database filter applied - returning all databases", available=len(all_databases)
        )
        return all_databases

    # Filter to requested databases
    filtered = {}
    for db_id in db_names:
        if db_id in all_databases:
            filtered[db_id] = all_databases[db_id]
        else:
            logger.warning("Requested database not found", db_id=db_id)

    logger.debug(
        "Database filter applied",
        requested=len(db_names),
        available=len(filtered),
        filtered_ids=list(filtered.keys()),
    )

    return filtered


def get_database_prompt(db_names: Optional[List[str]] = None) -> str:
    """
    Generate a filtered database prompt for agents.

    Args:
        db_names: List of database IDs to include

    Returns:
        Formatted database prompt with only the filtered databases
    """
    # Load databases directly without duplicate logging
    all_databases = get_available_databases()

    # Apply same filtering logic but without logging
    if not db_names:
        filtered_dbs = all_databases
    else:
        filtered_dbs = {db_id: all_databases[db_id] for db_id in db_names if db_id in all_databases}

    if not filtered_dbs:
        return "No databases available for this query."

    # Build the prompt
    prompt_parts = ["Available Financial Databases:\n"]

    for db_id, db_config in filtered_dbs.items():
        prompt_parts.append(f"\n{db_config['name']}:")
        prompt_parts.append(db_config.get("content", ""))

    return "\n".join(prompt_parts)
