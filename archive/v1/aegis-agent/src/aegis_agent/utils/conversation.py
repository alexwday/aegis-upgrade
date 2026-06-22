"""
Conversation processing module.

This module handles the processing and validation of incoming conversation data
for the Aegis agent workflow system.
"""

from typing import Any, Dict, Optional

from .logging import get_logger
from .settings import config


def process_conversation(  # pylint: disable=too-many-branches
    # Multiple validation branches needed to handle various input formats and filtering rules.
    conversation_input: Any,
    execution_id: str,
) -> Dict[str, Any]:
    """
    Process and validate incoming conversation data.

    Takes raw conversation input and applies the following processing steps:
    1. Validates message structure (role and content required)
    2. Filters messages by role based on configuration:
       - Removes system messages if INCLUDE_SYSTEM_MESSAGES=false
       - Only keeps roles listed in ALLOWED_ROLES
    3. Trims to MAX_HISTORY_LENGTH most recent messages (default: 10)
    4. Returns processed messages with metadata

    Args:
        conversation_input: Raw conversation data from API call. Accepts either:
                          1. {"messages": [{"role": str, "content": str}, ...]}
                          2. [{"role": str, "content": str}, ...] (will be wrapped)
        execution_id: Unique identifier for this execution.

    Returns:
        Processed conversation data with status and metadata:
        - "success": bool - Whether processing succeeded
        - "messages": list - Filtered & trimmed messages (only if success=True)
        - "message_count": int - Count after filtering/trimming
        - "original_message_count": int - Count before processing
        - "latest_message": dict - Last message after processing
        - "execution_id": str - Execution ID
        - "error": str or None - Error message if processing failed
        - "decision_details": str - Human-readable description

        Note: latest_message is the chronologically last message after filtering/trimming,
        regardless of role. Could be "user", "assistant", or "system" (if included).
    """
    logger = get_logger()
    logger.debug("Processing conversation")

    try:
        # Track original message count before any processing
        original_count = 0

        # Handle different input formats
        if isinstance(conversation_input, list):
            # If conversation is just a list, wrap it in a dict
            original_count = len(conversation_input)
            conversation_input = {"messages": conversation_input}
        elif not isinstance(conversation_input, dict):
            raise ValueError(f"Expected dict or list, got {type(conversation_input).__name__}")
        else:
            # It's already a dict, get the original count
            original_count = len(conversation_input.get("messages", []))

        # Extract messages
        if "messages" not in conversation_input:
            raise ValueError("Missing required 'messages' field")

        messages = conversation_input["messages"]

        if not isinstance(messages, list):
            raise ValueError("Messages must be a list")

        if not messages:
            raise ValueError("Messages list cannot be empty")

        # Validate and filter messages
        processed_messages = []
        for idx, message in enumerate(messages):
            processed_msg = _validate_and_filter_message(message, idx)
            if processed_msg:  # Only add if message passes filtering
                processed_messages.append(processed_msg)

        if not processed_messages:
            raise ValueError("No valid messages after filtering")

        # Keep only the most recent messages based on config
        if len(processed_messages) > config.max_history_length:
            processed_messages = processed_messages[-config.max_history_length :]  # noqa: E203

        # Extract the latest message (what we need to respond to)
        latest_message = processed_messages[-1]

        # Log processing results
        logger.info(
            "Conversation processed",
            message_count=len(processed_messages),
            latest_role=latest_message["role"],
        )

        # Prepare latest message preview
        latest_content = latest_message.get("content", "")[:50]
        if len(latest_message.get("content", "")) > 50:
            latest_content += "..."

        return {
            "success": True,
            "status": "Success",
            "messages": processed_messages,
            "latest_message": latest_message,
            "message_count": len(processed_messages),
            "original_message_count": original_count,
            "execution_id": execution_id,
            "error": None,
            "decision_details": (
                f"Messages in: {original_count}, out: {len(processed_messages)}, "
                f"latest: '{latest_content}'"
            ),
        }

    except Exception as e:  # pylint: disable=broad-exception-caught
        # Must catch all exceptions to return structured error response for workflow resilience.
        error_msg = str(e)
        logger.error("Failed to process conversation", error=error_msg)

        # Try to get original count for error response
        # Note: conversation_input will always be dict here since lists are converted at line 61
        original_count = 0
        if isinstance(conversation_input, dict):
            original_count = len(conversation_input.get("messages", []))

        return {
            "success": False,
            "status": "Failure",
            "messages": [],
            "latest_message": {},
            "message_count": 0,
            "original_message_count": original_count,
            "execution_id": execution_id,
            "error": error_msg,
            "decision_details": f"Conversation processing failed: {error_msg}",
        }


def _validate_and_filter_message(message: Any, index: int) -> Optional[Dict[str, str]]:
    """
    Validate and filter a single message based on configuration.

    Args:
        message: Message to validate.
        index: Position in the messages list (for error reporting).

    Returns:
        Validated message with role and content, or None if filtered out.

    Raises:
        ValueError: If message structure is invalid.
    """
    if not isinstance(message, dict):
        raise ValueError(f"Message at index {index} must be a dict")

    # Check required fields
    if "role" not in message:
        raise ValueError(f"Message at index {index} missing 'role' field")

    if "content" not in message:
        raise ValueError(f"Message at index {index} missing 'content' field")

    role = message["role"]
    content = message["content"]

    # Validate role against all possible roles
    valid_roles = {"system", "user", "assistant"}
    if role not in valid_roles:
        raise ValueError(
            f"Message at index {index} has invalid role '{role}'. " f"Must be one of: {valid_roles}"
        )

    # Validate content
    if not isinstance(content, str):
        raise ValueError(f"Message at index {index} content must be a string")

    if not content.strip():
        raise ValueError(f"Message at index {index} content cannot be empty")

    # Filter based on configuration
    # Check if role is allowed
    if role == "system":
        if not config.include_system_messages:
            return None  # Filter out system messages if configured
    elif role not in config.allowed_roles:
        return None  # Filter out roles not in allowed list

    return {"role": role, "content": content}
