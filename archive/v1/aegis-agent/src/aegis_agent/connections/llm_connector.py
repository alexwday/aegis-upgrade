"""
LLM connector module for OpenAI API integration.

This module handles all interactions with OpenAI's API, supporting both
OAuth and API key authentication, with configurable model tiers.
Fully async implementation with proper timeouts and error handling.
"""

from typing import Any, AsyncGenerator, Dict, List, Optional
import time
import httpx
from openai import AsyncOpenAI

from ..utils.logging import get_logger
from ..utils.settings import config

# Module-level client cache to reuse connections
_async_client_cache: Dict[str, AsyncOpenAI] = {}


# Cost tracking utilities integrated directly
def _calculate_cost(
    usage: Dict,
    cost_per_1k_input: float,
    cost_per_1k_output: Optional[float] = None,
    response_time: float = 0.0,
    model: str = "",
) -> Dict:
    """
    Calculate cost metrics from token usage.

    Args:
        usage: Usage dictionary from API response containing token counts
        cost_per_1k_input: Cost per 1000 input tokens in USD
        cost_per_1k_output: Cost per 1000 output tokens in USD (None for embeddings)
        response_time: Time taken for the API call in seconds
        model: Model name used for the operation

    Returns:
        Dictionary with calculated costs and metrics
    """
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens", prompt_tokens)

    # Calculate prompt cost
    prompt_cost = (prompt_tokens / 1000.0) * cost_per_1k_input

    # Calculate completion cost (if applicable)
    completion_cost = None
    if completion_tokens is not None and cost_per_1k_output is not None:
        completion_cost = (completion_tokens / 1000.0) * cost_per_1k_output
        total_cost = prompt_cost + completion_cost
    else:
        # For embeddings, only prompt cost
        total_cost = prompt_cost

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_cost": round(prompt_cost, 6),
        "completion_cost": round(completion_cost, 6) if completion_cost else None,
        "total_cost": round(total_cost, 6),
        "response_time": round(response_time, 3),
        "model": model,
    }


def _format_cost_for_logging(metrics: Dict) -> Dict:
    """
    Format metrics for structured logging output.

    Args:
        metrics: Dictionary of metrics to format

    Returns:
        Dictionary formatted for logging
    """
    # Simplified format - single line instead of nested dicts
    log_data = {
        "cost": f"${metrics['total_cost']:.6f}",
    }

    return log_data


class ResponseTimer:
    """
    Context manager for timing API responses.
    """

    def __init__(self):
        """Initialize the timer."""
        self.start_time = None
        self.elapsed = 0.0

    def __enter__(self):
        """Start the timer."""
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop the timer and calculate elapsed time."""
        self.elapsed = time.time() - self.start_time
        return False


def _calculate_and_log_metrics(
    usage: Dict[str, Any], model_tier: str, context: Dict[str, Any], operation_type: str
) -> Dict[str, Any]:
    """
    Calculate cost metrics and log them.

    Args:
        usage: Usage dictionary from API response
        model_tier: Model tier (small, medium, large)
        context: Context with model, response_time, execution_id, logger
        operation_type: Type of operation for log message

    Returns:
        Metrics dictionary
    """
    model_config = getattr(config.llm, model_tier)
    metrics = _calculate_cost(
        usage=usage,
        cost_per_1k_input=model_config.cost_per_1k_input,
        cost_per_1k_output=model_config.cost_per_1k_output,
        response_time=context["response_time"],
        model=context["model"],
    )

    # Simplified logging - just show key metrics, not full usage details
    log_data = {
        "execution_id": context["execution_id"],
        "model": context["model"],
        "tokens": usage.get("total_tokens", 0),
        "response_time_ms": int(context["response_time"] * 1000),
        **_format_cost_for_logging(metrics),
    }

    context["logger"].info(f"LLM {operation_type} successful", **log_data)

    return metrics


def _calculate_embedding_metrics(
    usage: Dict[str, Any], context: Dict[str, Any], operation_type: str
) -> Dict[str, Any]:
    """
    Calculate cost metrics for embeddings and log them.

    Args:
        usage: Usage dictionary from API response
        context: Context with model, response_time, execution_id, logger, vector_info
        operation_type: Type of operation for log message

    Returns:
        Metrics dictionary
    """
    metrics = _calculate_cost(
        usage=usage,
        cost_per_1k_input=config.llm.embedding.cost_per_1k_input,
        cost_per_1k_output=None,  # Embeddings don't have output tokens
        response_time=context["response_time"],
        model=context["model"],
    )

    log_data = {
        "execution_id": context["execution_id"],
        "model": context["model"],
        "usage": usage,
        **context.get("vector_info", {}),
        **_format_cost_for_logging(metrics),
    }

    context["logger"].info(f"{operation_type} successful", **log_data)

    return metrics


def _get_model_config(
    model: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    default_tier: str = "medium",
) -> tuple:
    """
    Determine model configuration based on model name.

    Args:
        model: Model name or None
        temperature: Temperature override or None
        max_tokens: Max tokens override or None
        default_tier: Default tier if model is None ("small", "medium", "large")

    Returns:
        Tuple of (model, temperature, max_tokens, model_tier, reasoning_effort)
    """
    if model is None:
        tier_config = getattr(config.llm, default_tier)
        return (
            tier_config.model,
            temperature or tier_config.temperature,
            max_tokens or tier_config.max_tokens,
            default_tier,
            tier_config.reasoning_effort,
        )

    # Determine tier from model name
    if model == config.llm.small.model:
        return (
            model,
            temperature or config.llm.small.temperature,
            max_tokens or config.llm.small.max_tokens,
            "small",
            config.llm.small.reasoning_effort,
        )
    if model == config.llm.large.model:
        return (
            model,
            temperature or config.llm.large.temperature,
            max_tokens or config.llm.large.max_tokens,
            "large",
            config.llm.large.reasoning_effort,
        )
    if model == config.llm.medium.model:
        return (
            model,
            temperature or config.llm.medium.temperature,
            max_tokens or config.llm.medium.max_tokens,
            "medium",
            config.llm.medium.reasoning_effort,
        )
    # Unknown model, use medium defaults
    return (
        model,
        temperature or config.llm.medium.temperature,
        max_tokens or config.llm.medium.max_tokens,
        "medium",
        config.llm.medium.reasoning_effort,
    )


def _uses_completion_token_limit(model: str) -> bool:
    """Return whether a model requires max_completion_tokens instead of max_tokens."""
    normalized = (model or "").lower()
    return (
        normalized in {"o1", "o3", "o4"}
        or normalized.startswith("o1-")
        or normalized.startswith("o3-")
        or normalized.startswith("o4-")
        or normalized.startswith("gpt-5")
    )


def _supports_reasoning_effort(model: str) -> bool:
    """Return whether the selected model supports reasoning_effort."""
    return _uses_completion_token_limit(model)


def _request_reasoning_effort(model: str, reasoning_effort: Optional[str]) -> Optional[str]:
    """Return reasoning_effort only when it will be sent to the API."""
    if reasoning_effort and _supports_reasoning_effort(model):
        return reasoning_effort
    return None


def _apply_generation_limits(
    api_params: Dict[str, Any],
    model: str,
    temperature: float,
    max_tokens: Optional[int],
    reasoning_effort: Optional[str] = None,
) -> None:
    """Apply generation parameters compatible with the selected Chat Completions model."""
    if _uses_completion_token_limit(model):
        if max_tokens:
            api_params["max_completion_tokens"] = max_tokens
        request_reasoning_effort = _request_reasoning_effort(model, reasoning_effort)
        if request_reasoning_effort:
            api_params["reasoning_effort"] = request_reasoning_effort
        return

    api_params["temperature"] = temperature
    api_params["max_tokens"] = max_tokens


async def _get_or_create_async_client(
    auth_token: str, ssl_config: Optional[Dict[str, Any]] = None
) -> AsyncOpenAI:
    """
    Get or create an async OpenAI client with proper configuration.

    Creates a cached AsyncOpenAI client configured with the appropriate
    authentication, timeout, and SSL settings. Clients are cached by auth token
    and SSL config to enable connection reuse.

    Args:
        auth_token: Authentication token from auth config
        ssl_config: Optional SSL configuration with 'verify'

    Returns:
        Configured AsyncOpenAI client instance.
    """
    logger = get_logger()

    # Create cache key from auth token and SSL config
    ssl_verify = ssl_config.get("verify", True) if ssl_config else True
    cache_key = f"{auth_token or 'no-auth'}_{ssl_verify}"

    # Return cached client if exists
    if cache_key in _async_client_cache:
        logger.debug(
            "Using cached async LLM client",
            ssl_verify=ssl_verify,
        )
        return _async_client_cache[cache_key]

    # Configure httpx client with SSL settings
    httpx_client_kwargs = {}
    if ssl_config:
        if not ssl_config.get("verify", True):
            # SSL verification disabled
            httpx_client_kwargs["verify"] = False
        # else: use default SSL verification after setup_ssl() has enabled
        # rbc_security certificates.

    # Create httpx client with SSL configuration
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=5.0), **httpx_client_kwargs
    )

    # Create AsyncOpenAI client with configured httpx client
    client = AsyncOpenAI(
        api_key=auth_token,
        base_url=config.llm.base_url,
        http_client=http_client,
        max_retries=3,
    )

    # Cache the client
    _async_client_cache[cache_key] = client

    logger.info(
        "Created new async LLM client",
        base_url=config.llm.base_url,
        timeout=180,
        ssl_verify=ssl_verify,
    )

    return client


async def complete(
    messages: List[Dict[str, str]],
    context: Dict[str, Any],
    llm_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate a non-streaming completion from the LLM.

    Makes an async call to the OpenAI API and returns the complete
    response. Suitable for simple question-answering and short responses.

    Args:
        messages: List of message dictionaries with 'role' and 'content'.
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration
        llm_params: Optional LLM parameters:
                    - model: Model to use (defaults to medium tier)
                    - temperature: Temperature setting
                    - max_tokens: Maximum tokens
                    - Additional OpenAI API parameters

    Returns:
        Response dictionary containing the completion.

    Raises:
        Exception: If the API call fails.
    """
    logger = get_logger()
    llm_params = llm_params or {}

    # Get model configuration using helper
    model, temperature, max_tokens, model_tier, configured_reasoning_effort = (
        _get_model_config(
            llm_params.get("model"),
            llm_params.get("temperature"),
            llm_params.get("max_tokens"),
        )
    )
    reasoning_effort = llm_params.get("reasoning_effort", configured_reasoning_effort)
    request_reasoning_effort = _request_reasoning_effort(model, reasoning_effort)

    logger.info(
        "Generating async LLM completion",
        execution_id=context["execution_id"],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        configured_reasoning_effort=reasoning_effort,
        reasoning_effort=request_reasoning_effort,
        reasoning_effort_applied=bool(request_reasoning_effort),
        message_count=len(messages),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        # Build API parameters based on model type
        api_params = {
            "model": model,
            "messages": messages,
        }
        _apply_generation_limits(api_params, model, temperature, max_tokens, reasoning_effort)

        # Add any extra parameters
        api_params.update(
            {
                k: v
                for k, v in llm_params.items()
                if k not in ["model", "temperature", "max_tokens", "reasoning_effort"]
            }
        )

        # Time the API call
        with ResponseTimer() as timer:
            response = await client.chat.completions.create(**api_params)

        # Convert response to dict
        response_dict = response.model_dump()

        # Calculate and log metrics
        response_dict["metrics"] = _calculate_and_log_metrics(
            usage=response_dict.get("usage", {}),
            model_tier=model_tier,
            context={
                "model": model,
                "response_time": timer.elapsed,
                "execution_id": context["execution_id"],
                "logger": logger,
            },
            operation_type="async completion",
        )

        return response_dict

    except Exception as e:
        logger.error(
            "Async LLM completion failed",
            execution_id=context["execution_id"],
            model=model,
            error=str(e),
        )
        raise


async def stream(
    messages: List[Dict[str, str]],
    context: Dict[str, Any],
    llm_params: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Generate a streaming completion from the LLM.

    Makes an async streaming call to the OpenAI API and yields chunks as they
    arrive. Suitable for long responses where you want to show progress.

    Args:
        messages: List of message dictionaries with 'role' and 'content'.
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration
        llm_params: Optional LLM parameters:
                    - model: Model to use (defaults to medium tier)
                    - temperature: Temperature setting
                    - max_tokens: Maximum tokens
                    - Additional OpenAI API parameters

    Yields:
        Response chunks as they arrive from the API.

    Raises:
        Exception: If the API call fails.
    """
    logger = get_logger()
    llm_params = llm_params or {}

    # Get model configuration using helper
    model, temperature, max_tokens, model_tier, configured_reasoning_effort = (
        _get_model_config(
            llm_params.get("model"),
            llm_params.get("temperature"),
            llm_params.get("max_tokens"),
        )
    )
    reasoning_effort = llm_params.get("reasoning_effort", configured_reasoning_effort)
    request_reasoning_effort = _request_reasoning_effort(model, reasoning_effort)

    logger.info(
        "Starting async LLM streaming",
        execution_id=context["execution_id"],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        configured_reasoning_effort=reasoning_effort,
        reasoning_effort=request_reasoning_effort,
        reasoning_effort_applied=bool(request_reasoning_effort),
        message_count=len(messages),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        # Start timing
        start_time = time.time()

        # Build API parameters based on model type
        api_params = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        _apply_generation_limits(api_params, model, temperature, max_tokens, reasoning_effort)

        # Add any extra parameters
        api_params.update(
            {
                k: v
                for k, v in llm_params.items()
                if k not in ["model", "temperature", "max_tokens", "reasoning_effort"]
            }
        )

        # Create async stream
        stream_response = await client.chat.completions.create(**api_params)

        chunk_count = 0
        accumulated_usage = None

        async for chunk in stream_response:
            chunk_count += 1
            chunk_dict = chunk.model_dump()

            # Accumulate usage from the final chunk (if present)
            if chunk_dict.get("usage"):
                accumulated_usage = chunk_dict["usage"]

            yield chunk_dict

        # Calculate elapsed time
        elapsed = time.time() - start_time

        # Log streaming completion
        if accumulated_usage:
            _calculate_and_log_metrics(
                usage=accumulated_usage,
                model_tier=model_tier,
                context={
                    "model": model,
                    "response_time": elapsed,
                    "execution_id": context["execution_id"],
                    "logger": logger,
                },
                operation_type=f"async streaming completed (chunks={chunk_count})",
            )
        else:
            logger.info(
                "Async LLM streaming completed",
                execution_id=context["execution_id"],
                model=model,
                chunks=chunk_count,
                response_time=elapsed,
            )

    except Exception as e:
        logger.error(
            "Async LLM streaming failed",
            execution_id=context["execution_id"],
            model=model,
            error=str(e),
        )
        raise


async def complete_with_tools(
    messages: List[Dict[str, str]],
    tools: List[Dict[str, Any]],
    context: Dict[str, Any],
    llm_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate a completion with tool/function calling capabilities.

    Makes an async call to the OpenAI API with tools defined, allowing the model
    to call functions and return structured responses.

    Args:
        messages: List of message dictionaries with 'role' and 'content'.
        tools: List of tool definitions for function calling.
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration
        llm_params: Optional LLM parameters:
                    - model: Model to use (defaults to large tier for tools)
                    - temperature: Temperature setting
                    - max_tokens: Maximum tokens
                    - Additional OpenAI API parameters

    Returns:
        Response dictionary containing the completion with tool calls.

    Raises:
        Exception: If the API call fails.
    """
    logger = get_logger()
    llm_params = llm_params or {}

    # Get model configuration using helper (default to large for tools)
    model, temperature, max_tokens, model_tier, configured_reasoning_effort = (
        _get_model_config(
            llm_params.get("model"),
            llm_params.get("temperature"),
            llm_params.get("max_tokens"),
            default_tier="large",  # Tools need better reasoning
        )
    )
    reasoning_effort = llm_params.get("reasoning_effort", configured_reasoning_effort)
    request_reasoning_effort = _request_reasoning_effort(model, reasoning_effort)

    logger.info(
        "Generating async LLM completion with tools",
        execution_id=context["execution_id"],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        configured_reasoning_effort=reasoning_effort,
        reasoning_effort=request_reasoning_effort,
        reasoning_effort_applied=bool(request_reasoning_effort),
        message_count=len(messages),
        tool_count=len(tools),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        # Build API parameters based on model type
        api_params = {
            "model": model,
            "messages": messages,
            "tools": tools,
        }
        _apply_generation_limits(api_params, model, temperature, max_tokens, reasoning_effort)

        # Add any extra parameters
        api_params.update(
            {
                k: v
                for k, v in llm_params.items()
                if k not in ["model", "temperature", "max_tokens", "reasoning_effort"]
            }
        )

        # Time the API call
        with ResponseTimer() as timer:
            response = await client.chat.completions.create(**api_params)

        # Convert response to dict
        response_dict = response.model_dump()

        # Check if tools were called
        has_tool_calls = bool(
            response_dict.get("choices", [{}])[0].get("message", {}).get("tool_calls")
        )

        # Calculate and log metrics
        response_dict["metrics"] = _calculate_and_log_metrics(
            usage=response_dict.get("usage", {}),
            model_tier=model_tier,
            context={
                "model": model,
                "response_time": timer.elapsed,
                "execution_id": context["execution_id"],
                "logger": logger,
            },
            operation_type=f"async tool completion (has_tool_calls={has_tool_calls})",
        )

        return response_dict

    except Exception as e:
        logger.error(
            "Async LLM tool completion failed",
            execution_id=context["execution_id"],
            model=model,
            error=str(e),
        )
        raise


async def stream_with_tools(
    messages: List[Dict[str, str]],
    tools: List[Dict[str, Any]],
    context: Dict[str, Any],
    llm_params: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream a Chat Completions response with tool-calling enabled.

    This lets the UI receive assistant text deltas while still allowing the
    agent loop to accumulate tool-call deltas and dispatch tools after the
    streamed model step completes.
    """
    logger = get_logger()
    llm_params = llm_params or {}

    model, temperature, max_tokens, model_tier, configured_reasoning_effort = (
        _get_model_config(
            llm_params.get("model"),
            llm_params.get("temperature"),
            llm_params.get("max_tokens"),
            default_tier="large",
        )
    )
    reasoning_effort = llm_params.get("reasoning_effort", configured_reasoning_effort)
    request_reasoning_effort = _request_reasoning_effort(model, reasoning_effort)

    logger.info(
        "Starting async LLM streaming with tools",
        execution_id=context["execution_id"],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        configured_reasoning_effort=reasoning_effort,
        reasoning_effort=request_reasoning_effort,
        reasoning_effort_applied=bool(request_reasoning_effort),
        message_count=len(messages),
        tool_count=len(tools),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        api_params = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        _apply_generation_limits(api_params, model, temperature, max_tokens, reasoning_effort)

        api_params.update(
            {
                key: value
                for key, value in llm_params.items()
                if key not in ["model", "temperature", "max_tokens", "reasoning_effort"]
            }
        )

        start_time = time.time()
        stream_response = await client.chat.completions.create(**api_params)
        chunk_count = 0
        accumulated_usage = None

        async for chunk in stream_response:
            chunk_count += 1
            chunk_dict = chunk.model_dump()
            if chunk_dict.get("usage"):
                accumulated_usage = chunk_dict["usage"]
            yield chunk_dict

        elapsed = time.time() - start_time
        if accumulated_usage:
            _calculate_and_log_metrics(
                usage=accumulated_usage,
                model_tier=model_tier,
                context={
                    "model": model,
                    "response_time": elapsed,
                    "execution_id": context["execution_id"],
                    "logger": logger,
                },
                operation_type=f"async tool streaming completed (chunks={chunk_count})",
            )
        else:
            logger.info(
                "Async LLM tool streaming completed",
                execution_id=context["execution_id"],
                model=model,
                chunks=chunk_count,
                response_time=elapsed,
            )

    except Exception as e:
        logger.error(
            "Async LLM tool streaming failed",
            execution_id=context["execution_id"],
            model=model,
            error=str(e),
        )
        raise


async def embed(
    input_text: str,
    context: Dict[str, Any],
    embedding_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate embedding vector for input text.

    Creates a vector representation of the input text using OpenAI's
    embedding models. Suitable for similarity search, clustering, and
    other vector operations.

    Args:
        input_text: Text to generate embedding for.
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration
        embedding_params: Optional embedding parameters:
                          - model: Embedding model to use (defaults to configured model)
                          - dimensions: Vector dimensions (for models that support it)
                          - Additional OpenAI API parameters

    Returns:
        Response dictionary containing the embedding vector.

    Raises:
        Exception: If the API call fails.
    """
    logger = get_logger()

    # Extract embedding parameters with defaults
    if embedding_params is None:
        embedding_params = {}

    # Get embedding configuration
    model = embedding_params.get("model", config.llm.embedding.model)
    dimensions = embedding_params.get("dimensions", config.llm.embedding.dimensions)

    # Remove our known params, pass rest as kwargs
    kwargs = {k: v for k, v in embedding_params.items() if k not in ["model", "dimensions"]}

    # Add dimensions if supported by the model
    if "text-embedding-3" in model and dimensions:
        kwargs["dimensions"] = dimensions

    logger.info(
        "Generating async text embedding",
        execution_id=context["execution_id"],
        model=model,
        dimensions=dimensions if "text-embedding-3" in model else "default",
        input_length=len(input_text),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        # Time the API call
        with ResponseTimer() as timer:
            response = await client.embeddings.create(model=model, input=input_text, **kwargs)

        # Convert response to dict
        response_dict = response.model_dump()

        # Calculate and log metrics
        response_dict["metrics"] = _calculate_embedding_metrics(
            usage=response_dict.get("usage", {}),
            context={
                "model": model,
                "response_time": timer.elapsed,
                "execution_id": context["execution_id"],
                "logger": logger,
                "vector_info": {"vector_length": len(response_dict["data"][0]["embedding"])},
            },
            operation_type="Async embedding generation",
        )

        return response_dict

    except Exception as e:
        logger.error(
            "Async embedding generation failed",
            execution_id=context["execution_id"],
            model=model,
            error=str(e),
        )
        raise


async def embed_batch(
    input_texts: List[str],
    context: Dict[str, Any],
    embedding_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate embeddings for multiple texts in a single API call.

    Creates vector representations for multiple input texts efficiently
    using OpenAI's batch embedding capability.

    Args:
        input_texts: List of texts to generate embeddings for.
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration
        embedding_params: Optional embedding parameters:
                          - model: Embedding model to use (defaults to configured model)
                          - dimensions: Vector dimensions (for models that support it)
                          - Additional OpenAI API parameters

    Returns:
        Response dictionary containing embedding vectors for all inputs.

    Raises:
        Exception: If the API call fails.
    """
    logger = get_logger()

    # Extract embedding parameters with defaults
    if embedding_params is None:
        embedding_params = {}

    # Get embedding configuration
    model = embedding_params.get("model", config.llm.embedding.model)
    dimensions = embedding_params.get("dimensions", config.llm.embedding.dimensions)

    # Remove our known params, pass rest as kwargs
    kwargs = {k: v for k, v in embedding_params.items() if k not in ["model", "dimensions"]}

    # Add dimensions if supported by the model
    if "text-embedding-3" in model and dimensions:
        kwargs["dimensions"] = dimensions

    logger.info(
        "Generating async batch embeddings",
        execution_id=context["execution_id"],
        model=model,
        dimensions=dimensions if "text-embedding-3" in model else "default",
        batch_size=len(input_texts),
        total_chars=sum(len(text) for text in input_texts),
    )

    try:
        client = await _get_or_create_async_client(
            context["auth_config"].get("token", "no-token"), context.get("ssl_config")
        )

        # Time the API call
        with ResponseTimer() as timer:
            response = await client.embeddings.create(model=model, input=input_texts, **kwargs)

        # Convert response to dict
        response_dict = response.model_dump()

        # Calculate and log metrics
        response_dict["metrics"] = _calculate_embedding_metrics(
            usage=response_dict.get("usage", {}),
            context={
                "model": model,
                "response_time": timer.elapsed,
                "execution_id": context["execution_id"],
                "logger": logger,
                "vector_info": {
                    "vectors_generated": len(response_dict["data"]),
                    "vector_length": (
                        len(response_dict["data"][0]["embedding"]) if response_dict["data"] else 0
                    ),
                },
            },
            operation_type="Async batch embedding generation",
        )

        return response_dict

    except Exception as e:
        logger.error(
            "Async batch embedding generation failed",
            execution_id=context["execution_id"],
            model=model,
            batch_size=len(input_texts),
            error=str(e),
        )
        raise


async def check_connection(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check the LLM connection with a simple prompt.

    Sends a basic test message to verify that authentication and
    connectivity are working properly.

    Args:
        context: Runtime context containing:
                 - execution_id: Unique identifier for this execution
                 - auth_config: Authentication configuration
                 - ssl_config: SSL configuration

    Returns:
        Test response with status and details.
    """
    logger = get_logger()

    logger.info(
        "Testing async LLM connection",
        execution_id=context["execution_id"],
        auth_method=context["auth_config"].get("method"),
        base_url=config.llm.base_url,
    )

    test_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say 'Hello! I'm working properly.' and nothing else."},
    ]

    try:
        # Use small model for testing (faster and cheaper)
        response = await complete(
            messages=test_messages,
            context=context,
            llm_params={
                "model": config.llm.small.model,
                "temperature": 0,  # Deterministic for testing
                "max_tokens": 50,
            },
        )

        content = response["choices"][0]["message"]["content"]

        result = {
            "status": "success",
            "model": config.llm.small.model,
            "response": content,
            "auth_method": context["auth_config"].get("method"),
            "base_url": config.llm.base_url,
        }

        logger.info(
            "Async LLM connection test successful",
            execution_id=context["execution_id"],
            response=content,
        )

        return result

    except Exception as e:  # pylint: disable=broad-exception-caught
        # Connection check must catch all errors to report any connectivity issues without crashing.
        result = {
            "status": "failed",
            "error": str(e),
            "auth_method": context["auth_config"].get("method"),
            "base_url": config.llm.base_url,
        }

        logger.error(
            "Async LLM connection test failed",
            execution_id=context["execution_id"],
            error=str(e),
        )

        return result


# Cleanup function for graceful shutdown
async def close_all_clients():
    """
    Close all cached async OpenAI clients.

    This should be called during application shutdown to ensure
    proper cleanup of async resources.
    """
    global _async_client_cache

    logger = get_logger()
    logger.info(f"Closing {len(_async_client_cache)} async LLM client(s)")

    # Close all clients
    for key, client in _async_client_cache.items():
        try:
            await client.close()
        except Exception as e:
            logger.error(f"Error closing client {key[:8]}...: {e}")

    # Clear the cache
    _async_client_cache.clear()
