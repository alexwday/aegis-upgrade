"""LLM connector with swappable auth (OAuth or API key)."""

import copy
import logging
from threading import Lock
from typing import Any

from openai import OpenAI

from ..utils.config_setup import (
    get_api_key,
    get_llm_auth_mode,
    get_llm_endpoint,
    get_oauth_config,
    get_stage_model_config,
)
from .oauth_connector import OAuthClient

logger = logging.getLogger(__name__)

_HEALTH_CHECK_PROMPT: dict[str, Any] = {
    "stage": "startup",
    "system_prompt": (
        "You are a health check agent. You must respond "
        "using the provided tool. Do not respond with text."
    ),
    "user_prompt": "Respond with status ok.",
    "tool_choice": "required",
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Health check",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        }
    ],
}


def _prepare_tools(tools: list[dict]) -> list[dict]:
    """Return cloned OpenAI tools with strict function schemas enabled."""
    prepared = copy.deepcopy(tools)
    for tool in prepared:
        if tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        function["strict"] = True
    return prepared


def _prepare_tool_choice(
    tool_choice: str | dict,
    tools: list[dict],
) -> str | dict:
    """Return an explicit single-function choice when tools require one call."""
    if tool_choice != "required":
        return tool_choice
    function_tools = [
        tool.get("function", {})
        for tool in tools
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict)
    ]
    if len(function_tools) != 1:
        return tool_choice
    function_name = function_tools[0].get("name")
    if not isinstance(function_name, str) or not function_name:
        return tool_choice
    return {"type": "function", "function": {"name": function_name}}


class LLMClient:
    """OpenAI-compatible LLM client with pluggable auth.

    Supports OAuth token refresh or a static API key, controlled by the
    LLM_AUTH_MODE environment setting through config_setup.get_llm_auth_mode.
    The constructor reads local configuration but does not make a chat or
    an OAuth token refresh until call or test_connection is invoked.
    """

    def __init__(self, verify_ssl: bool = True):
        """Create a configured client wrapper for later LLM requests."""
        self.auth_mode = get_llm_auth_mode()
        self.endpoint = get_llm_endpoint()
        self.verify_ssl = verify_ssl
        self.oauth_client = None
        self.oauth_openai_client = None
        self.oauth_token = ""
        self.static_client = None
        self._client_lock = Lock()

        if self.auth_mode == "oauth":
            oauth_cfg = get_oauth_config()
            self.oauth_client = OAuthClient(
                config=oauth_cfg,
                verify_ssl=self.verify_ssl,
            )
            logger.debug("LLM client configured with OAuth")
        else:
            api_key = get_api_key()
            self.static_client = OpenAI(
                api_key=api_key,
                base_url=self.endpoint,
            )
            logger.debug("LLM client configured with API key")

    def get_client(self) -> OpenAI:
        """Build or return an OpenAI client with current auth.

        Static API-key mode returns the prebuilt client. OAuth mode may perform
        a live token request, then caches an OpenAI client for the active token.
        """
        if self.static_client:
            return self.static_client
        token = self.oauth_client.get_token()
        with self._client_lock:
            if self.oauth_openai_client is not None and self.oauth_token == token:
                return self.oauth_openai_client
            self.oauth_openai_client = OpenAI(
                api_key=token,
                base_url=self.endpoint,
            )
            self.oauth_token = token
            return self.oauth_openai_client

    def call(
        self,
        messages: list,
        stage: str = "startup",
        tools: list | None = None,
        tool_choice: str | dict | None = None,
        context: str = "",
    ) -> dict:
        """Make an LLM tool-calling request.

        Model settings are read from environment-derived stage configuration.
        tool definitions are cloned and strict mode is enabled before dispatch.
        This method performs a live chat-completions request and returns the full API
        response as a dict, raising SDK or configuration errors on failure.
        """
        client = self.get_client()
        model_config = get_stage_model_config(stage)

        kwargs = {
            "model": model_config["model"],
            "messages": messages,
            "max_completion_tokens": model_config["max_tokens"],
        }
        if model_config["temperature"] is not None:
            kwargs["temperature"] = model_config["temperature"]
        if model_config.get("reasoning_effort") is not None:
            kwargs["reasoning_effort"] = model_config["reasoning_effort"]
        prepared_tools = _prepare_tools(tools) if tools else []
        if prepared_tools:
            kwargs["tools"] = prepared_tools
        if tool_choice:
            kwargs["tool_choice"] = _prepare_tool_choice(
                tool_choice,
                prepared_tools,
            )

        log_parts = []
        if context:
            log_parts.append(context)
        log_parts.append(f"model={model_config['model']}")
        log_parts.append(f"max_tokens={model_config['max_tokens']}")
        if model_config["temperature"] is not None:
            log_parts.append(f"temp={model_config['temperature']}")
        log_parts.append(f"messages={len(messages)}")
        if tools:
            log_parts.append(f"tools={len(tools)}")

        logger.debug(
            "LLM call: %s",
            ", ".join(log_parts),
            extra={"stage": stage},
        )

        response = client.chat.completions.create(**kwargs)
        return response.model_dump()

    def embed(
        self,
        texts: list[str],
        model: str,
        dimensions: int = 0,
    ) -> list[list[float]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: Non-empty text strings to embed. Empty input returns an
                empty list without making a live request.
            model: Embedding model name to send to the configured endpoint.
            dimensions: Optional positive vector dimension override supported
                by OpenAI text embedding models.

        Returns:
            Embedding vectors in the same order as ``texts``.

        External side effects:
            Performs a live embeddings API request.
        """
        if not texts:
            return []
        client = self.get_client()
        kwargs: dict[str, Any] = {"model": model, "input": texts}
        if dimensions > 0:
            kwargs["dimensions"] = dimensions

        logger.debug(
            "LLM embedding call: model=%s items=%d dimensions=%s",
            model,
            len(texts),
            dimensions or "default",
            extra={"stage": "embedding"},
        )
        response = client.embeddings.create(**kwargs)
        sorted_data = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in sorted_data]

    def test_connection(self) -> bool:
        """Validate LLM connectivity with a tool-calling request.

        Sends a minimal live chat-completions request and returns True only
        when the model responds with the expected tool-call structure. Raises
        the underlying exception when the check fails.
        """
        prompt = _HEALTH_CHECK_PROMPT
        messages = []
        if prompt.get("system_prompt"):
            messages.append({"role": "system", "content": prompt["system_prompt"]})
        messages.append({"role": "user", "content": prompt["user_prompt"]})
        try:
            response = self.call(
                messages=messages,
                stage=prompt["stage"],
                tools=prompt.get("tools"),
                tool_choice=prompt.get("tool_choice"),
            )
            choices = response.get("choices", [])
            tool_calls = (
                choices[0].get("message", {}).get("tool_calls") if choices else None
            )
            if not tool_calls:
                raise RuntimeError("LLM did not return a tool call")
            logger.debug("LLM connection test passed")
            return True
        except Exception:
            logger.error("LLM connection test failed")
            raise
