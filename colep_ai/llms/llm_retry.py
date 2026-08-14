"""
colep_ai/ingestion/claude_retry.py

Exponential backoff retry utility for LLM API calls.

Wraps any callable that may raise a RateLimitError from either
Anthropic or OpenAI and retries it up to MAX_RETRIES times with
exponential backoff: 60s -> 120s -> 240s.

After MAX_RETRIES exhausted, raises the original exception cleanly
with a meaningful error string — no double-wrapping, no swallowed tracebacks.

Usage:
    from colep_ai.ingestion.claude_retry import with_anthropic_retry, with_openai_retry

    # Anthropic
    response = with_anthropic_retry(lambda: client.messages.create(...))

    # OpenAI
    response = with_openai_retry(lambda: client.chat.completions.create(...))
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

import anthropic
from openai import RateLimitError as OpenAIRateLimitError

from colep_ai.core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
# Backoff delays in seconds: attempt 1 -> 60s, attempt 2 -> 120s, attempt 3 -> 240s
BACKOFF_DELAYS = [60, 120, 240]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _retry(
    fn: Callable[[], T],
    rate_limit_exc_type: type,
    caller_label: str,
) -> T:
    """
    Core retry loop. Not called directly — use the typed wrappers below.

    Args:
        fn:                  Zero-argument callable wrapping the API call.
        rate_limit_exc_type: Exception class to catch (RateLimitError variant).
        caller_label:        String identifying the caller for log messages.

    Returns:
        Whatever fn() returns on success.

    Raises:
        The original RateLimitError after MAX_RETRIES exhausted.
        Any non-rate-limit exception immediately — no retry.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except rate_limit_exc_type as exc:
            if attempt == MAX_RETRIES:
                logger.error(
                    f"[retry] {caller_label} | rate limit hit on attempt {attempt}/{MAX_RETRIES} "
                    f"— no more retries | {type(exc).__name__}: {exc}"
                )
                raise

            delay = BACKOFF_DELAYS[attempt - 1]
            logger.warning(
                f"[retry] {caller_label} | rate limit hit on attempt {attempt}/{MAX_RETRIES} "
                f"— waiting {delay}s before retry | {type(exc).__name__}: {exc}"
            )
            time.sleep(delay)

    # Should never reach here
    raise RuntimeError(f"[retry] {caller_label} | unexpected exit from retry loop")


# ---------------------------------------------------------------------------
# Typed wrappers
# ---------------------------------------------------------------------------

def with_anthropic_retry(fn: Callable[[], T], caller_label: str = "anthropic") -> T:
    """
    Retry an Anthropic API call on RateLimitError with exponential backoff.

    Args:
        fn:           Zero-argument callable: lambda: client.messages.create(...)
        caller_label: Optional label for log messages (e.g. "flowchart_extractor").

    Returns:
        The Anthropic response object.

    Raises:
        anthropic.RateLimitError after MAX_RETRIES exhausted.
        Any other exception immediately without retry.
    """
    return _retry(fn, anthropic.RateLimitError, caller_label)


def with_openai_retry(fn: Callable[[], T], caller_label: str = "openai") -> T:
    """
    Retry an OpenAI API call on RateLimitError with exponential backoff.

    Args:
        fn:           Zero-argument callable: lambda: client.chat.completions.create(...)
        caller_label: Optional label for log messages (e.g. "classify_page_azure").

    Returns:
        The OpenAI response object.

    Raises:
        openai.RateLimitError after MAX_RETRIES exhausted.
        Any other exception immediately without retry.
    """
    return _retry(fn, OpenAIRateLimitError, caller_label)
