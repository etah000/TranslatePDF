"""pdf2zh_next — minimal translation-engine library used by serve.py.

Only the two supported engines (OpenAI / ClaudeCode), the QPS rate
limiter, the translation cache, and the two Pydantic engine-settings
models are exposed. Everything else (Gradio UI, CLI, SettingsModel
pydantic configuration, subprocess wrapper, 22 unused translator
adapters) was removed when the project was refactored to a single-file
FastAPI service. See USER_GUIDE.md for the public API.
"""

from pdf2zh_next.config.translate_engine_model import ClaudeCodeSettings
from pdf2zh_next.config.translate_engine_model import OpenAISettings
from pdf2zh_next.translator.base_rate_limiter import BaseRateLimiter
from pdf2zh_next.translator.rate_limiter.qps_rate_limiter import QPSRateLimiter
from pdf2zh_next.translator.translator_impl.claudecode import ClaudeCodeTranslator
from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator

__version__ = "2.9.0"
__author__ = "Byaidu, awwaawwa"
__license__ = "AGPL-3.0"
__maintainer__ = "awwaawwa"
__email__ = "aw@funstory.ai"

__all__ = [
    "OpenAISettings",
    "ClaudeCodeSettings",
    "BaseRateLimiter",
    "QPSRateLimiter",
    "OpenAITranslator",
    "ClaudeCodeTranslator",
]
