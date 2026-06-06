"""LLM utility — provider-agnostic chat completion.

Migrated from legacy ``training/llm_utils.py``. Wraps the anthropic / openai /
ollama branching shared by ETGBuilder / PlanBuilder / PlanEvaluator into a
single function. A FakeLLM test double only needs to implement ``.call(system,
user)`` to be injectable.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class LLMRequiredError(RuntimeError):
    """Raised when a Builder requires an LLM but none was injected."""


def call_llm(
    *,
    client: Any,
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> str | None:
    """Per-provider call. FakeLLM is used first if it exposes a ``.call()`` method.

    Returns: raw text or None (failure / empty response).
    """
    if client is None:
        raise LLMRequiredError("LLM client is not configured")

    # Test double — FakeLLM exposes .call(system, user)
    if hasattr(client, "call") and callable(client.call):
        try:
            return client.call(system_prompt, user_message)
        except Exception as e:  # noqa: BLE001
            logger.error("FakeLLM call failed: %s", e)
            return None

    try:
        if provider == "anthropic":
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            for block in message.content:
                if hasattr(block, "text"):
                    return block.text
            return None

        if provider in ("openai", "ollama"):
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content or None
    except Exception as e:  # noqa: BLE001
        logger.error("LLM (%s) call failed: %s", provider, e)
        return None

    return None


def parse_json_response(raw: str | None) -> Any | None:
    """Strip fenced markdown and parse JSON. Return None on failure."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError as e:
        logger.warning("LLM JSON parse failed: %s", e)
        return None


__all__ = ["LLMRequiredError", "call_llm", "parse_json_response"]
