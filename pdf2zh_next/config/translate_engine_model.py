"""Pydantic settings models for the two supported translation engines.

Only OpenAI and ClaudeCode are kept; the upstream package supported 24
adapters (DeepL, Bing, Google, Azure, Ollama, etc.) which were dropped when
the project was refactored down to a single FastAPI service.
"""

from __future__ import annotations

import logging
import re
import typing

from pydantic import BaseModel
from pydantic import Field
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)


def _clean_string(value: str | None) -> str | None:
    """Trim whitespace from a string field."""
    if value is None:
        return None
    return value.strip()


def _clean_url(value: str | None) -> str | None:
    """Normalise an OpenAI-compatible base URL.

    Strips trailing slashes and the ``/chat/completions`` suffix so that
    values entered in ``config.json`` can be either the bare root or a
    full chat-completions URL.
    """
    if value is None:
        return None
    cleaned = value.strip().rstrip("/")
    cleaned = re.sub(r"/chat/completions/?$", "", cleaned)
    return cleaned.rstrip("/")


def _check_if_positive_float(value: str | None, field: str = "Value") -> str | None:
    """Validate that *value* is a string parseable as a positive float."""
    if value is None:
        return None
    try:
        f = float(value)
    except ValueError as e:
        raise ValueError(f"{field} must be a float") from e
    if f <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return value


class TranslateEngineSettingError(Exception):
    """Raised when an engine setting is invalid."""


# ---------------------------------------------------------------------------
# OpenAI (and any OpenAI-compatible provider)
# ---------------------------------------------------------------------------
class OpenAISettings(BaseModel):
    """OpenAI API settings.

    Compatible with any provider that speaks the OpenAI chat-completions
    protocol: real OpenAI, Azure-OpenAI-proxy, vLLM, Ollama's OpenAI shim,
    DeepSeek, MiniMax, Zhipu, etc. Switch provider by setting
    ``openai_model`` and ``openai_base_url``; ``translate_engine_type``
    must stay ``"OpenAI"`` (this is the engine class identifier, not the
    model name).
    """

    _openai_extra_body: typing.Any | None = PrivateAttr(default=None)

    translate_engine_type: typing.Literal["OpenAI"] = Field(default="OpenAI")
    support_llm: typing.Literal["yes", "no"] = Field(
        default="yes", description="Whether the translator supports LLM"
    )

    openai_model: str = Field(default="gpt-4o-mini", description="OpenAI model to use")
    openai_base_url: str | None = Field(
        default=None, description="Base URL for OpenAI API"
    )
    openai_api_key: str | None = Field(
        default=None, description="API key for OpenAI service"
    )
    openai_timeout: str | None = Field(
        default=None, description="Timeout (seconds) for OpenAI service"
    )
    openai_temperature: str | None = Field(
        default=None, description="Temperature for OpenAI service"
    )
    openai_reasoning_effort: str | None = Field(
        default=None,
        description="Reasoning effort for OpenAI service (minimal/low/medium/high)",
    )
    openai_enable_json_mode: bool | None = Field(
        default=None, description="Enable JSON mode for OpenAI service"
    )

    # This parameter contains a spelling error, but it will not be corrected
    # for compatibility reasons. See:
    # https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/175
    openai_send_temprature: bool | None = Field(
        default=None, description="Send temprature to OpenAI service"
    )
    openai_send_reasoning_effort: bool | None = Field(
        default=None, description="Send reasoning effort to OpenAI service"
    )

    def validate_settings(self) -> None:
        if not self.openai_api_key:
            raise ValueError("OpenAI API key is required")
        self.openai_api_key = _clean_string(self.openai_api_key)
        self.openai_base_url = _clean_url(self.openai_base_url)
        self.openai_model = _clean_string(self.openai_model)
        self.openai_timeout = _check_if_positive_float(
            _clean_string(self.openai_timeout),
            field="Timeout",
        )
        self.openai_temperature = _clean_string(self.openai_temperature)
        self.openai_reasoning_effort = _clean_string(self.openai_reasoning_effort)
        if self.openai_send_temprature:
            if not self.openai_temperature:
                raise ValueError(
                    "Temperature is required when send temperature is enabled"
                )
            try:
                float(self.openai_temperature)
            except ValueError as e:
                raise ValueError("Temperature must be a float") from e
        if self.openai_send_reasoning_effort and not self.openai_reasoning_effort:
            raise ValueError(
                "Reasoning effort is required when send reasoning effort is enabled"
            )


# ---------------------------------------------------------------------------
# ClaudeCode
# ---------------------------------------------------------------------------
class ClaudeCodeSettings(BaseModel):
    """Claude Code CLI settings.

    Translation is delegated to the local ``claude`` CLI binary, which must
    be installed and on ``$PATH`` (or reachable via ``claude_code_path``).
    """

    translate_engine_type: typing.Literal["ClaudeCode"] = Field(default="ClaudeCode")
    claude_code_path: str = Field(
        default="claude", description="Path to Claude Code CLI"
    )
    claude_code_model: str = Field(
        default="sonnet", description="Claude Code model to use"
    )

    def validate_settings(self) -> None:
        if not self.claude_code_path:
            raise ValueError("Claude Code path is required")
