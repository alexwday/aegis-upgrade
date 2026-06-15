"""
SQL-based prompt management - retrieves prompts from PostgreSQL database.
"""

from sqlalchemy import (
    create_engine,
    Table,
    Column,
    Integer,
    MetaData,
    Text,
    TIMESTAMP,
    func,
    JSON,
    ARRAY,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
import pandas as pd
import yaml
import dotenv
import logging
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone
from .settings import config
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load PostgreSQL connection variable from variables from .env file
dotenv.load_dotenv()

POSTGRES_HOST = config.postgres_host
POSTGRES_PORT = config.postgres_port
POSTGRES_DATABASE = config.postgres_database
POSTGRES_USER = config.postgres_user
POSTGRES_PASSWORD = config.postgres_password

DB_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DATABASE}"


class SQLPromptManager:
    def __init__(self, model_filter: str = None):
        self.engine = create_engine(DB_URL)
        self.metadata = MetaData()
        self.prompts_table = None
        self.model_filter = model_filter
        self._define_prompts_table()
        self.df_prompts = self._df_prompts(
            model=self.model_filter
        )  # Latest prompts DataFrame for unique model/layer/name based

    def _define_prompts_table(self):
        self.prompts_table = Table(
            "prompts",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("model", Text, nullable=False),
            Column("layer", Text),
            Column("name", Text, nullable=False),
            Column("description", Text),
            Column("comments", Text),
            Column("system_prompt", Text),
            Column("user_prompt", Text),
            Column("tool_definition", JSON),
            Column("uses_global", ARRAY(Text)),
            Column("version", Text, default="1.0.0"),
            Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
            Column(
                "updated_at",
                TIMESTAMP(timezone=True),
                server_default=func.now(),
                onupdate=func.now(),
            ),
        )

    def create_prompts_table(self):
        try:
            # Check if table already exists
            with self.engine.connect() as connection:
                table_exists = connection.dialect.has_table(connection, "prompts")

            if table_exists:
                logger.info("Prompts table already exists")
            else:
                # Create the table if it doesn't exist
                self.metadata.create_all(self.engine, checkfirst=True)
                logger.info("Prompts table created successfully")
            return True

        except SQLAlchemyError as e:
            logger.error(f"Error creating prompts table: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error creating prompts table: {str(e)}")
            return False

    def _df_prompts(self, model: str = None):
        if model:
            query = """
            SELECT p.*
            FROM prompts p
            INNER JOIN (
                SELECT model, layer, name, MAX(updated_at) as max_updated_at
                FROM prompts
                WHERE model = %(model)s
                GROUP BY model, layer, name
            ) latest
            ON p.model = latest.model
            AND p.layer = latest.layer
            AND p.name = latest.name
            AND p.updated_at = latest.max_updated_at
            ORDER BY p.id
            """
            df = pd.read_sql(query, self.engine, params={"model": model})
        else:
            query = """
            SELECT p.*
            FROM prompts p
            INNER JOIN (
                SELECT model, layer, name, MAX(updated_at) as max_updated_at
                FROM prompts
                GROUP BY model, layer, name
            ) latest
            ON p.model = latest.model
            AND p.layer = latest.layer
            AND p.name = latest.name
            AND p.updated_at = latest.max_updated_at
            ORDER BY p.id
            """
            df = pd.read_sql(query, self.engine)
        return df

    def get_latest_prompt(
        self, model: str = None, layer: str = None, name: str = None, system_prompt: bool = True
    ):
        try:
            # Check if all mandatory parameters are provided
            if not model or not layer or not name:
                missing_params = []
                if not model:
                    missing_params.append("model")
                if not layer:
                    missing_params.append("layer")
                if not name:
                    missing_params.append("name")
                logger.warning(
                    f"Missing mandatory parameters: {', '.join(missing_params)}. All of model, layer, and name are required."
                )
                return "Blank"

            # Start with all prompts
            filtered_df = self.df_prompts.copy()

            # Filter by model, layer, and name (all are mandatory)
            filtered_df = filtered_df[
                (filtered_df["model"] == model)
                & (filtered_df["layer"] == layer)
                & (filtered_df["name"] == name)
            ]

            # Check if any results found
            if filtered_df.empty:
                logger.warning(f"No prompt found for model={model}, layer={layer}, name={name}")
                return "Blank"

            # Get the first row (should be the latest due to _df_prompts query)
            row = filtered_df.iloc[0]

            if system_prompt:
                try:
                    # Try to parse as YAML
                    parsed = yaml.safe_load(row["system_prompt"])
                    return parsed
                except (yaml.YAMLError, Exception) as yaml_err:
                    # If YAML parsing fails, return the raw string
                    logger.info("Returning system_prompt as raw text instead")
                    return row["system_prompt"]

            # Return full row as dict
            return row.to_dict()

        except Exception as e:
            logger.error(f"Error retrieving latest prompt from DataFrame: {str(e)}")
            return "Blank"


prompt_manager = None


def postgresql_prompts():
    global prompt_manager
    prompt_manager = SQLPromptManager(model_filter="aegis")
    return prompt_manager
