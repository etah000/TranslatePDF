"""FastAPI HTTP server for PDF translation.

Drives babeldoc's async_translate from a minimal HTML UI.
Reuses pdf2zh_next's OpenAITranslator / ClaudeCodeTranslator / cache / rate-limiter
without the Gradio WebUI, the SettingsModel, or the subprocess wrapper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# BabelDoc: provides parsing, splitting, layout, translation driver,
# cache, QPS rate limiter, and the BaseTranslator class we inherit from.
import babeldoc.assets.assets  # noqa: F401  warmup side-effect
import babeldoc.translator.cache  # noqa: F401  triggers init_db() at import
from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.translator.translator import BaseTranslator
from babeldoc.translator.translator import set_translate_rate_limiter


# ---------------------------------------------------------------------------
# Translators — two thin subclasses of BabelDoc's BaseTranslator.
# BabelDoc handles cache lookup, rate-limit wait, and `<think>` stripping;
# we only implement do_translate / do_llm_translate.
# ---------------------------------------------------------------------------
def _translation_prompt(text: str, lang_out: str) -> list[dict]:
    """Build the chat-completions message list for a translation request."""
    return [{
        "role": "user",
        "content": (
            f"You are a professional,authentic machine translation engine.\n\n"
            f";; Treat next line as plain text input and translate it into "
            f"{lang_out}, output translation ONLY. If translation is "
            f"unnecessary (e.g. proper nouns, codes, {{{{1}}}}, etc.), return "
            f"the original text. NO explanations. NO notes. Input:\n\n{text}"
        ),
    }]


def _check_pause(translator):
    """Block the calling thread if the task is paused.

    Called from ``do_translate`` / ``do_llm_translate`` (worker thread)
    before every API call, so the pause takes effect between paragraphs
    without losing any completed work.
    """
    ev = getattr(translator, "_pause_event", None)
    if ev is not None and not ev.is_set():
        ev.wait()


class OpenAITranslator(BaseTranslator):
    """OpenAI / OpenAI-compatible chat-completions translator."""

    name = "openai"

    def __init__(self, settings: SimpleNamespace, lang_in: str, lang_out: str):
        super().__init__(lang_in, lang_out, ignore_cache=settings.ignore_cache)
        self.client = openai.OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout=float(settings.openai_timeout) if settings.openai_timeout else openai.NOT_GIVEN,
        )
        self.model: str = settings.openai_model
        self.options: dict[str, Any] = {}
        if settings.openai_send_temprature and settings.openai_temperature:
            self.options["temperature"] = float(settings.openai_temperature)
            self.add_cache_impact_parameters("temperature", self.options["temperature"])
        if settings.openai_send_reasoning_effort and settings.openai_reasoning_effort:
            self.options["reasoning_effort"] = settings.openai_reasoning_effort
            self.add_cache_impact_parameters("reasoning_effort", self.options["reasoning_effort"])
        if settings.openai_enable_json_mode:
            self.add_cache_impact_parameters("enable_json_mode", True)
        self.add_cache_impact_parameters("model", self.model)
        self.add_cache_impact_parameters("prompt", _translation_prompt("", self.lang_out)[0]["content"])

    @staticmethod
    def _remove_cot_content(text: str) -> str:
        """Strip ``<think>...</think>`` blocks that reasoning models prepend.

        ``BaseTranslator._remove_cot_content`` no longer exists in babeldoc
        0.6.x (it was removed when the upstream OpenAITranslator stopped
        assuming the model emits CoT tokens).  Our local translator still
        needs the behaviour because we point it at reasoning models like
        ``MiniMax-M3`` whose chat completions start with a ``<think>`` block
        before the actual translation.  Without stripping, every paragraph
        starts with the model's scratchpad and the output PDF is unreadable.
        """
        if not text:
            return ""
        # Non-greedy + DOTALL so the block can span newlines; case-insensitive
        # in case the model uppercases the tags.  Leave any text outside the
        # block untouched and trim residual whitespace.
        return re.sub(
            r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE
        ).strip()

    def _call(self, messages: list[dict], response_format_json: bool = False) -> str:
        import openai as _openai

        opts = self.options.copy()
        if response_format_json and self.options.get("enable_json_mode"):
            opts["response_format"] = {"type": "json_object"}

        # Retry on transient rate-limit errors with exponential back-off.
        # Quota-exhausted (insufficient_quota) is NOT retried — it won't
        # recover without human intervention.
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                r = self.client.chat.completions.create(
                    model=self.model, **opts, messages=messages,
                )
                break
            except _openai.RateLimitError as e:
                body = getattr(e, "body", None) or {}
                if isinstance(body, dict):
                    inner = body.get("error", body)
                    if isinstance(inner, dict):
                        code = inner.get("code", "")
                        msg = str(inner.get("message", e))
                    else:
                        code = ""
                        msg = str(e)
                else:
                    code = ""
                    msg = str(e)
                # Quota exhaustion — no point retrying.
                if code == "insufficient_quota" or "insufficient_quota" in msg:
                    raise RuntimeError(
                        f"API quota exhausted for model '{self.model}'."
                        f" Switch to a different model or top up your account."
                    ) from e
                if attempt == max_retries:
                    raise
                wait = min(2 ** attempt, 30)
                time.sleep(wait)

        choice = r.choices[0]
        content = getattr(choice.message, "content", None)
        if not content:
            return ""
        return self._remove_cot_content(str(content).strip())

    def do_translate(self, text, rate_limit_params=None):
        _check_pause(self)
        return self._call(_translation_prompt(text, self.lang_out))

    def do_llm_translate(self, text, rate_limit_params=None):
        # translator_supports_llm calls do_llm_translate(None) to probe
        # whether the translator supports LLM mode.  Sending content=null
        # to the API is wasteful and can trigger 400 errors from stricter
        # providers (DeepSeek).  Return NotImplementedError immediately
        # so babeldoc falls back to ILTranslator (paragraph-at-a-time).
        if text is None:
            raise NotImplementedError("llm_translate probe")
        _check_pause(self)
        return self._call(
            [{"role": "user", "content": text}],
            response_format_json=bool(
                rate_limit_params and rate_limit_params.get("request_json_mode")
            ),
        )


class ClaudeCodeTranslator(BaseTranslator):
    """Translator that shells out to the local `claude` CLI."""

    name = "claudecode"

    def __init__(self, settings: SimpleNamespace, lang_in: str, lang_out: str):
        super().__init__(lang_in, lang_out, ignore_cache=settings.ignore_cache)
        self.cli_path: str = settings.claude_code_path
        self.model: str = settings.claude_code_model
        self._test_cli()
        self.add_cache_impact_parameters("model", self.model)
        self.add_cache_impact_parameters("prompt", _translation_prompt("", self.lang_out)[0]["content"])

    def _test_cli(self) -> None:
        try:
            r = subprocess.run(
                [self.cli_path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                raise ValueError(f"claude CLI error: {r.stderr}")
        except FileNotFoundError as e:
            raise ValueError(
                f"claude CLI not found at '{self.cli_path}'"
            ) from e

    @staticmethod
    def _parse_stream_json(output: str) -> str:
        chunks: list[str] = []
        for line in output.strip().splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant" and "message" in ev:
                for part in ev["message"].get("content", []):
                    if part.get("type") == "text":
                        chunks.append(part.get("text", ""))
            elif ev.get("type") == "text":
                chunks.append(ev.get("text", ""))
        result = "".join(chunks).strip()
        if not result:
            raise ValueError("No translation received from Claude Code")
        return result

    def do_translate(self, text, rate_limit_params=None):
        messages = _translation_prompt(text, self.lang_out)
        cmd = [
            self.cli_path, "-p",
            "--model", self.model,
            "--max-turns", "1",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--disallowedTools",
            "Task Bash Glob Grep LS exit_plan_mode Read Edit MultiEdit Write "
            "NotebookRead NotebookEdit TodoRead TodoWrite",
        ]
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env,
            )
            stdout, stderr = proc.communicate(
                input=json.dumps({"type": "user", "message": messages[0]}),
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr)
        return self._parse_stream_json(stdout)

    def do_llm_translate(self, text, rate_limit_params=None):
        return self.do_translate(text, rate_limit_params)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = APP_ROOT / "uploads"
OUTPUT_DIR = APP_ROOT / "outputs"
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
CONFIG_PATH = Path(os.environ.get("PDF2ZH_CONFIG", DEFAULT_CONFIG_PATH))

_TRANSLATION_KEYS = frozenset({"translation", "server"})


def _get_configured_models() -> list[str]:
    """Return model names present in config.json (excluding meta sections)."""
    try:
        raw = json.loads(CONFIG_PATH.read_bytes())
    except Exception:
        return []
    return [
        key for key in raw
        if key not in _TRANSLATION_KEYS
        and key != "active_model"
        and isinstance(raw[key], dict)
        and ("openai_api_key" in raw[key] or "claude_code_path" in raw[key])
    ]
# Defaults for the UI (lang_in=en, lang_out=zh). Both can be overridden
# per-request; values must come from the whitelists below.
DEFAULT_SOURCE_LANG = "en"
DEFAULT_TARGET_LANG = "zh"
# Whitelist of source languages (BabelDoc supports many more; this is the
# subset surfaced in the UI to keep things small).
ALLOWED_SOURCE_LANGS = (
    "auto", "en", "zh", "ja", "fr", "de", "es", "ru", "ko", "pt", "it",
)
ALLOWED_TARGET_LANGS = (
    "en", "zh", "ja", "fr", "de", "es", "ru", "ko", "pt", "it",
)
WATERMARK_MAP = {
    "watermarked": WatermarkOutputMode.Watermarked,
    "no_watermark": WatermarkOutputMode.NoWatermark,
    "no watermark": WatermarkOutputMode.NoWatermark,
    "nowatermark": WatermarkOutputMode.NoWatermark,
    "both": WatermarkOutputMode.Both,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("serve")

# Lift BabelDoc internals from WARNING (default) to INFO so users see what
# each stage is actually doing — useful for diagnosing performance/quality
# issues without re-running with DEBUG noise.
_BABELDOC_LOGGERS = (
    "babeldoc",
    "babeldoc.format.pdf.high_level",
    "babeldoc.format.pdf.document_il",
    "babeldoc.format.pdf.document_il.midend",
    "babeldoc.format.pdf.document_il.midend.il_translator",
    "babeldoc.format.pdf.document_il.midend.il_translator_llm_only",
    "babeldoc.format.pdf.document_il.midend.layout_parser",
    "babeldoc.format.pdf.document_il.midend.paragraph_finder",
    "babeldoc.format.pdf.document_il.midend.automatic_term_extractor",
    "babeldoc.format.pdf.document_il.midend.detect_scanned_file",
    "babeldoc.format.pdf.document_il.midend.styles_and_formulas",
    "babeldoc.format.pdf.document_il.midend.table_parser",
    "babeldoc.format.pdf.document_il.midend.typesetting",
    "babeldoc.format.pdf.document_il.backend",
    "babeldoc.format.pdf.document_il.backend.pdf_creater",
    "babeldoc.format.pdf.new_parser",
    "babeldoc.translator",
    "babeldoc.translator.translator",
    "pdf2zh_next.translator",
)
for _name in _BABELDOC_LOGGERS:
    logging.getLogger(_name).setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# In-memory task table
# ---------------------------------------------------------------------------
class Task:
    __slots__ = (
        "task_id", "pdf_path", "output_dir", "status",
        "events", "result", "error", "asyncio_task",
        "lang_in", "lang_out", "model_override",
        "pause_event",  # threading.Event — set when paused
    )

    def __init__(self, task_id: str, pdf_path: Path, output_dir: Path,
                 lang_in: str = "", lang_out: str = "",
                 model_override: str | None = None):
        self.task_id = task_id
        self.pdf_path = pdf_path
        self.output_dir = output_dir
        self.status = "pending"   # pending / running / done / error / cancelled / paused
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.result: dict | None = None
        self.error: str | None = None
        self.asyncio_task: asyncio.Task | None = None  # populated when worker starts
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.model_override = model_override
        self.pause_event = threading.Event()
        self.pause_event.set()  # start in "not paused" state
        # pause_event.is_set() → not paused; pause_event.wait() blocks when paused


TASKS: dict[str, Task] = {}
TASK_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class AppConfig:
    """Holds validated translator settings derived from config.json."""

    def __init__(self, raw: dict):
        self.active_model: str = raw.get("active_model", "openai")
        # Engine settings as a flat dict. Each translator reads only the keys
        # it knows about; defaults below match what OpenAISettings /
        # ClaudeCodeSettings used to provide.
        model_cfg = raw.get(self.active_model, {})
        if isinstance(model_cfg, dict) and "openai_api_key" in model_cfg:
            self.engine: dict = {
                "openai_model":         "gpt-4o-mini",
                "openai_base_url":      None,
                "openai_api_key":       None,
                "openai_timeout":       None,
                "openai_temperature":   None,
                "openai_reasoning_effort": None,
                "openai_send_temprature":   False,
                "openai_send_reasoning_effort": False,
                "openai_enable_json_mode": False,
                **model_cfg,
            }
            if not self.engine["openai_api_key"]:
                raise ValueError(f"{self.active_model}.openai_api_key is required")
            if self.engine["openai_timeout"] is not None:
                float(self.engine["openai_timeout"])  # raises if invalid
        else:
            self.engine: dict = {
                "claude_code_path":  "claude",
                "claude_code_model": "sonnet",
                **raw.get("claudecode", {}),
            }
            if not self.engine["claude_code_path"]:
                raise ValueError("claudecode.claude_code_path is required")
        # Translation defaults
        tr = raw.get("translation", {})
        self.qps: int = int(tr.get("qps", 4))
        self.ignore_cache: bool = bool(tr.get("ignore_cache", False))
        self.no_dual: bool = bool(tr.get("no_dual", False))
        self.no_mono: bool = bool(tr.get("no_mono", False))
        self.watermark_str: str = tr.get("watermark_output_mode", "watermarked")
        if self.watermark_str not in WATERMARK_MAP:
            raise ValueError(f"watermark_output_mode must be one of {list(WATERMARK_MAP)}")
        self.min_text_length: int = int(tr.get("min_text_length", 5))
        # Server
        sv = raw.get("server", {})
        self.host: str = sv.get("host", "0.0.0.0")
        self.port: int = int(sv.get("port", 8765))

    def build_settings(self) -> SimpleNamespace:
        """A flat settings namespace the translators read by attribute."""
        s = SimpleNamespace(**self.engine)
        s.ignore_cache = self.ignore_cache
        return s


def load_config(path: Path) -> AppConfig:
    log.info("Loading config from %s", path)
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return AppConfig(raw)


CONFIG: AppConfig | None = None
CONFIG_LOCK = asyncio.Lock()


async def get_config() -> AppConfig:
    global CONFIG
    async with CONFIG_LOCK:
        if CONFIG is None:
            CONFIG = load_config(CONFIG_PATH)
        return CONFIG


async def reload_config() -> AppConfig:
    global CONFIG
    async with CONFIG_LOCK:
        CONFIG = load_config(CONFIG_PATH)
        log.info("Config reloaded; active_model=%s", CONFIG.active_model)
        return CONFIG


# ---------------------------------------------------------------------------
# Translator factory
# ---------------------------------------------------------------------------
def make_translator(cfg: AppConfig, settings: SimpleNamespace, lang_in: str, lang_out: str):
    # Any model with openai_* config keys uses the OpenAI-compatible translator.
    engine = getattr(cfg, "engine", {})
    if "openai_api_key" in engine:
        return OpenAITranslator(settings, lang_in, lang_out)
    return ClaudeCodeTranslator(settings, lang_in, lang_out)


# ---------------------------------------------------------------------------
# Translation worker
# ---------------------------------------------------------------------------
def _file_meta(path: Path) -> dict:
    """Cheap metadata for logging: size in MB, mtime."""
    try:
        size = path.stat().st_size
    except OSError:
        return {"size_bytes": -1}
    return {
        "size_bytes": size,
        "size_mb": round(size / (1024 * 1024), 2),
    }


async def run_translation(task: Task, lang_in: str, lang_out: str,
                         model_override: str | None = None) -> None:
    cfg = await get_config()
    if model_override and model_override != cfg.active_model:
        # Build a transient config for the requested model, reusing the same
        # translation/server settings from config.json.
        import copy
        cfg = copy.copy(cfg)
        cfg.active_model = model_override
        # engine dict is populated by __init__ based on active_model — redo.
        raw = json.loads(CONFIG_PATH.read_bytes())
        if "openai_api_key" in raw.get(model_override, {}):
            cfg.engine = {
                "openai_model":         "gpt-4o-mini",
                "openai_base_url":      None,
                "openai_api_key":       None,
                "openai_timeout":       None,
                "openai_temperature":   None,
                "openai_reasoning_effort": None,
                "openai_send_temprature":   False,
                "openai_send_reasoning_effort": False,
                "openai_enable_json_mode": False,
                **raw.get(model_override, {}),
            }
        else:
            cfg.engine = {
                "claude_code_path":  "claude",
                "claude_code_model": "sonnet",
                **raw.get(model_override, {}),
            }

    settings = cfg.build_settings()
    translator = make_translator(cfg, settings, lang_in, lang_out)
    term_translator = make_translator(cfg, settings, lang_in, lang_out)  # same engine
    # Wire the pause event into both translators so they block before API calls.
    translator._pause_event = task.pause_event
    term_translator._pause_event = task.pause_event
    set_translate_rate_limiter(max(1, cfg.qps))

    config = TranslationConfig(
        input_file=task.pdf_path,
        output_dir=task.output_dir,
        translator=translator,
        term_extraction_translator=term_translator,
        lang_in=lang_in,
        lang_out=lang_out,
        doc_layout_model=None,
        qps=cfg.qps,
        pool_max_workers=cfg.qps,
        no_dual=cfg.no_dual,
        no_mono=cfg.no_mono,
        watermark_output_mode=WATERMARK_MAP[cfg.watermark_str],
        min_text_length=cfg.min_text_length,
        report_interval=0.1,
        auto_extract_glossary=False,  # term extraction is slow; default off
    )
    # Reuse in-memory IL from a previous run (model-switch resume path).
    cached = getattr(task, "_cached_il", None)
    if cached is not None:
        config.cached_il = cached
        del task._cached_il  # one-shot
    task.status = "running"

    fm = _file_meta(task.pdf_path)
    log.info(
        "[%s] translation start | engine=%s | model=%s | %s→%s | qps=%d | "
        "watermark=%s | no_dual=%s | no_mono=%s | file=%s (%.2f MB)",
        task.task_id,
        cfg.active_model,
        getattr(cfg, "engine", {}).get(
                "openai_model" if "openai_api_key" in getattr(cfg, "engine", {}) else "claude_code_model",
                "?"),
        lang_in, lang_out, cfg.qps,
        cfg.watermark_str, cfg.no_dual, cfg.no_mono,
        task.pdf_path.name, fm.get("size_mb", 0.0),
    )

    # Per-stage timing: collect elapsed seconds keyed by BabelDOC stage name.
    # Emitted to the SSE stream as a `perf` event on finish/cancel/error so
    # the UI can show a breakdown.
    perf: dict[str, float] = {}
    current_stage: str | None = None
    current_stage_t0: float | None = None
    last_progress_pct: float = -1.0
    last_event_time: float = time.monotonic()
    run_t0 = time.monotonic()

    async def emit(event: dict) -> None:
        nonlocal last_event_time
        last_event_time = time.monotonic()
        await task.events.put(event)

    async def stall_watchdog() -> None:
        """Emit a warning if no progress event arrives for 60+ seconds."""
        while task.status == "running":
            await asyncio.sleep(30)
            if task.status != "running":
                break
            gap = time.monotonic() - last_event_time
            if gap > 60:
                log.warning(
                    "[%s] stalled: no progress event for %.0fs (stage=%s)",
                    task.task_id, gap, current_stage or "?",
                )
                await task.events.put({
                    "type": "progress_stall",
                    "elapsed_since_last_event": round(gap, 1),
                    "current_stage": current_stage or "unknown",
                })

    stall_task = asyncio.create_task(stall_watchdog())

    async def emit_perf(reason: str) -> None:
        await emit({
            "type": "perf",
            "reason": reason,
            "elapsed_total": round(time.monotonic() - run_t0, 3),
            "stages": {k: round(v, 3) for k, v in perf.items()},
        })

    try:
        async for event in async_translate(config):
            # Honour pause: if the event is not cleared, wait here so the
            # SSE stream pauses and the worker thread blocks at the next
            # API call (see _check_pause above).
            if not task.pause_event.is_set():
                await asyncio.to_thread(task.pause_event.wait)
                task.pause_event.set()  # restore "not paused" state
                if task.status == "paused":
                    task.status = "running"
                    await emit({"type": "resumed", "task_id": task.task_id})

            etype = event.get("type")

            # ----- stage timing --------------------------------------------
            if etype == "stage_summary":
                # Log the full stage list with estimated weights on first arrival.
                stages = event.get("stages", [])
                if stages:
                    stage_list = ", ".join(
                        f"{s.get('name','?')}({s.get('percent',0)*100:.0f}%)"
                        for s in stages
                    )
                    log.info("[%s]   pipeline stages: %s", task.task_id, stage_list)
            elif etype == "progress_start":
                # Capture the in-memory IL when we enter the translation
                # stage so model-switch resume can reuse it.
                if event.get("stage", "").startswith("Translate"):
                    task._cached_il = getattr(config, "cached_il", None)

                new_stage = event.get("stage", "?")
                stage_total = event.get("stage_total")
                if new_stage != current_stage:
                    if current_stage is not None and current_stage_t0 is not None:
                        elapsed = time.monotonic() - current_stage_t0
                        perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
                        log.info(
                            "[%s]   stage END  %s (%.2fs)",
                            task.task_id, current_stage, elapsed,
                        )
                    current_stage = new_stage
                    current_stage_t0 = time.monotonic()
                    # Include cumulative elapsed so users can gauge how far
                    # through the pipeline they are.
                    cum = round(time.monotonic() - run_t0, 1)
                    log.info(
                        "[%s]   stage START %s | total_items=%s | cumulative_elapsed=%.1fs",
                        task.task_id, current_stage, stage_total, cum,
                    )
                    last_progress_pct = -1.0  # reset for new stage
            elif etype == "progress_end":
                if current_stage is not None and current_stage_t0 is not None:
                    elapsed = time.monotonic() - current_stage_t0
                    perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
                    items = event.get("stage_total", 0)
                    rate = f"{items / elapsed:.1f} items/s" if elapsed > 0 and items else ""
                    log.info(
                        "[%s]   stage END  %s (%.2fs, items=%s)%s",
                        task.task_id, current_stage, elapsed,
                        items, f" | {rate}" if rate else "",
                    )
            elif etype == "progress_update":
                pct = event.get("overall_progress")
                if isinstance(pct, (int, float)):
                    int_pct = int(pct)
                    # Log at every 10% transition (or every 5% for stages with
                    # few items), but always include stage-elapsed time.
                    sc = event.get("stage_current", 0)
                    st = event.get("stage_total", 0)
                    threshold = 5 if st <= 20 else 10
                    if int_pct != last_progress_pct and int_pct % threshold == 0:
                        stage_elapsed = (
                            f"stage_elapsed={time.monotonic() - current_stage_t0:.1f}s"
                            if current_stage_t0 else ""
                        )
                        rate_info = ""
                        if current_stage_t0 and sc > 0:
                            rate = sc / (time.monotonic() - current_stage_t0)
                            rate_info = f" | {rate:.1f} items/s"
                        log.info(
                            "[%s]     %s: %d%% (%d/%d)%s | %s",
                            task.task_id,
                            current_stage or "?",
                            int_pct, sc, st,
                            rate_info,
                            stage_elapsed,
                        )
                        last_progress_pct = int_pct

            # ----- fan-out to SSE ------------------------------------------
            event_out = {k: v for k, v in event.items() if k != "translate_result"}
            # Augment progress events with timing info for the UI.
            if etype in ("progress_start", "progress_update", "progress_end"):
                event_out["total_elapsed"] = round(time.monotonic() - run_t0, 1)
                if current_stage_t0 is not None:
                    event_out["stage_elapsed"] = round(time.monotonic() - current_stage_t0, 1)
            if etype == "finish":
                res = event["translate_result"]
                # babeldoc names outputs as <stem>.<lang>.<kind>.pdf already
                # (see pdf_creater.py:1456-1532).  With WatermarkOutputMode
                # "NoWatermark" the no_watermark_* attributes point at the
                # same paths as mono_/dual_ — just use the canonical keys.
                task.result = {
                    "mono_pdf_path": str(getattr(res, "mono_pdf_path", "") or ""),
                    "dual_pdf_path": str(getattr(res, "dual_pdf_path", "") or ""),
                    "total_seconds": float(getattr(res, "total_seconds", 0.0)),
                }
                event_out["result"] = task.result
                task.status = "done"
                # If the last stage didn't get a progress_end, close it now.
                if current_stage is not None and current_stage_t0 is not None:
                    elapsed = time.monotonic() - current_stage_t0
                    perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
                    current_stage = None
                    current_stage_t0 = None
                total = time.monotonic() - run_t0
                log.info(
                    "[%s] translation DONE in %.2fs | babeldoc_reported=%.2fs",
                    task.task_id, total, task.result["total_seconds"],
                )
                log.info(
                    "[%s] stage breakdown: %s",
                    task.task_id,
                    ", ".join(f"{k}={v:.2f}s" for k, v in perf.items()) or "(none)",
                )
            await emit(event_out)
    except asyncio.CancelledError:
        # Close the current stage so the perf breakdown is correct.
        if current_stage is not None and current_stage_t0 is not None:
            elapsed = time.monotonic() - current_stage_t0
            perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
        # Respect an already-set status (e.g. "paused" from the pause endpoint
        # or "cancelled" from a server-shutdown cancel).
        if task.status not in ("paused",):
            task.status = "cancelled"
        action = task.status  # "paused" or "cancelled"
        log.warning(
            "[%s] translation %s after %.2fs | partial_stages=%s",
            task.task_id, action.upper(), time.monotonic() - run_t0,
            {k: round(v, 2) for k, v in perf.items()},
        )
        await emit_perf(action)
        await emit({"type": action, "task_id": task.task_id})
        # Do not re-raise — during shutdown the event loop may already be
        # tearing down, and re-raising just produces an unhelpful traceback.
    except Exception as exc:  # noqa: BLE001
        if current_stage is not None and current_stage_t0 is not None:
            elapsed = time.monotonic() - current_stage_t0
            perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
        log.exception(
            "[%s] translation FAILED after %.2fs in stage=%r: %s",
            task.task_id, time.monotonic() - run_t0, current_stage, exc,
        )
        task.status = "error"
        task.error = str(exc)
        await emit_perf("error")
        await emit({"type": "error", "error": str(exc)})
    else:
        await emit_perf("done")
    finally:
        stall_task.cancel()
        # Remove the IL checkpoint if the task didn't finish successfully.
        _cp = task.output_dir / f"{task.pdf_path.stem}.il.pickle"
        if _cp.exists():
            try:
                _cp.unlink()
            except Exception:
                pass
        await task.events.put(None)  # sentinel


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    await get_config()
    try:
        yield
    finally:
        # Cancel all in-flight translation tasks so the thread pool and
        # subprocesses are released before the process exits.
        async with TASK_LOCK:
            for task_id, task in list(TASKS.items()):
                if task.asyncio_task and not task.asyncio_task.done():
                    task.asyncio_task.cancel()
                    log.info("[%s] cancelled due to server shutdown", task_id)
        # Give tasks a brief window to finish their cleanup.
        pending = [t.asyncio_task for t in TASKS.values()
                   if t.asyncio_task and not t.asyncio_task.done()]
        if pending:
            await asyncio.wait(pending, timeout=3)
        TASKS.clear()


app = FastAPI(title="PDF Translator", lifespan=lifespan)


@app.get("/health")
async def health():
    cfg = await get_config()
    return {"status": "ok", "active_model": cfg.active_model}


@app.get("/api/models")
async def api_models():
    cfg = await get_config()
    return {
        "models": _get_configured_models(),
        "default": cfg.active_model,
    }


@app.post("/api/reload-config")
async def api_reload_config():
    cfg = await reload_config()
    return {"ok": True, "active_model": cfg.active_model}


@app.post("/api/translate")
async def api_translate(
    file: UploadFile = File(...),
    lang_in: str = Form(...),
    lang_out: str = Form(...),
    model: str = Form(""),  # optional — any model from config.json
):
    # Validate languages
    if lang_in not in ALLOWED_SOURCE_LANGS:
        raise HTTPException(
            status_code=422,
            detail=f"lang_in {lang_in!r} not in {list(ALLOWED_SOURCE_LANGS)}",
        )
    if lang_out not in ALLOWED_TARGET_LANGS:
        raise HTTPException(
            status_code=422,
            detail=f"lang_out {lang_out!r} not in {list(ALLOWED_TARGET_LANGS)}",
        )
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="file must be a PDF")

    # Derive a safe stem from the original filename for human-readable outputs.
    raw_stem = Path(file.filename).stem
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_stem).strip("_") or "document"
    # Avoid overwriting a previous upload / output with the same stem.
    dedup = ""
    while (UPLOAD_DIR / f"{safe_stem}{dedup}.pdf").exists() or any(
        (OUTPUT_DIR / f"{safe_stem}{dedup}{s}").exists()
        for s in ("-mono.pdf", "-dual.pdf",
                  "-mono.no_watermark.pdf", "-dual.no_watermark.pdf")
    ):
        dedup = f"-{uuid.uuid4().hex[:6]}"
    stem = f"{safe_stem}{dedup}"

    task_id = uuid.uuid4().hex
    pdf_path = UPLOAD_DIR / f"{stem}.pdf"
    out_dir = OUTPUT_DIR  # no uuid subdir — babeldoc names files after the input stem

    # Write the uploaded file via the default thread pool so the event loop
    # stays free to serve SSE connections and health checks.
    loop = asyncio.get_running_loop()

    def _save_upload() -> None:
        with pdf_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

    await loop.run_in_executor(None, _save_upload)
    log.info("Saved upload %s -> %s (%.1f MB)", file.filename, pdf_path,
             pdf_path.stat().st_size / (1024 * 1024))

    task = Task(task_id, pdf_path, out_dir, lang_in, lang_out,
                model or None)
    # Push an immediate event so the UI shows feedback even before the first
    # babeldoc progress callback fires.
    await task.events.put({
        "type": "started",
        "task_id": task_id,
        "filename": file.filename,
        "model": model or (await get_config()).active_model,
    })
    async with TASK_LOCK:
        TASKS[task_id] = task
        task.asyncio_task = asyncio.create_task(
            run_translation(task, lang_in, lang_out, model or None)
        )
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
async def api_task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "result": task.result,
        "error": task.error,
    }


@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: str):
    """Cancel a running translation task (terminal — cannot be resumed)."""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in ("done", "error", "cancelled"):
        return {"task_id": task_id, "status": task.status, "already_finished": True}
    if task.status == "paused":
        # Move from paused to cancelled.
        task.status = "cancelled"
        await task.events.put(
            {"type": "cancelled", "task_id": task_id})
        await task.events.put(None)
        return {"task_id": task_id, "status": "cancelled"}
    if task.asyncio_task and not task.asyncio_task.done():
        task.asyncio_task.cancel()
        log.info("Cancel requested for task %s", task_id)
    return {"task_id": task_id, "status": "cancelling"}


@app.post("/api/tasks/{task_id}/pause")
async def api_task_pause(task_id: str):
    """Pause a running translation — suspends in-place, no progress lost."""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in ("done", "error", "cancelled", "paused"):
        return {"task_id": task_id, "status": task.status, "already_finished": True}
    # Clear the event → worker thread blocks before next API call,
    # event loop blocks before next progress event.
    task.pause_event.clear()
    task.status = "paused"
    log.info("Pause requested for task %s", task_id)
    await task.events.put({"type": "paused", "task_id": task_id})
    return {"task_id": task_id, "status": "paused"}


@app.post("/api/tasks/{task_id}/resume")
async def api_task_resume(task_id: str, request: Request):
    """Resume a paused translation task.

    If *model* query param is given and differs from the current model,
    the task is cancelled and restarted with the new model.  Translated
    paragraphs are served from cache, so only unfinished work consumes
    quota.
    """
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"task status is {task.status!r}, expected 'paused'",
        )

    new_model = request.query_params.get("model")
    model_changed = bool(
        new_model
        and new_model != (task.model_override or (await get_config()).active_model)
    )

    if model_changed:
        # Model switch — restart the pipeline with the new translator.
        # If we reached the translation stage on the previous run, the
        # in-memory IL is available and parsing stages are skipped.
        task.model_override = new_model
        task.pause_event.set()  # unblock old task so it can receive cancel
        if task.asyncio_task and not task.asyncio_task.done():
            task.asyncio_task.cancel()
        # Give the old task a moment to unwind its thread-pool work.
        await asyncio.sleep(0.5)
        task.events = asyncio.Queue()
        task.error = None
        await task.events.put({
            "type": "started",
            "task_id": task_id,
            "filename": task.pdf_path.name,
            "model": new_model,
        })
        async with TASK_LOCK:
            task.status = "running"
            task.asyncio_task = asyncio.create_task(
                run_translation(task, task.lang_in, task.lang_out, new_model)
            )
        return {"task_id": task_id, "status": "resumed", "model_changed": True}

    # Same model — just unpause in-place.  The worker thread and event
    # loop unblock without losing any progress.
    task.pause_event.set()
    task.status = "running"
    log.info("Resume requested for task %s (same model)", task_id)
    return {"task_id": task_id, "status": "resumed", "model_changed": False}


@app.get("/api/tasks/{task_id}/events")
async def api_task_events(task_id: str, request: Request):
    """Server-Sent Events stream of babeldoc progress events."""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    async def event_source():
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(task.events.get(), timeout=15.0)
            except asyncio.TimeoutError:
                # heartbeat keep-alive
                yield ": keepalive\n\n"
                continue
            if event is None:  # sentinel
                break
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_DOWNLOAD_KIND_TO_ATTR = {
    "dual": "dual_pdf_path",
    "mono": "mono_pdf_path",
}


@app.get("/api/tasks/{task_id}/download/{kind}")
async def api_download(task_id: str, kind: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if not task.result:
        raise HTTPException(status_code=409, detail="task not finished")
    attr = _DOWNLOAD_KIND_TO_ATTR.get(kind)
    if not attr:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(_DOWNLOAD_KIND_TO_ATTR)}")
    path_str = task.result.get(attr, "")
    if not path_str or not Path(path_str).exists():
        raise HTTPException(status_code=404, detail=f"{kind} pdf not produced")
    return FileResponse(
        path_str,
        media_type="application/pdf",
        filename=Path(path_str).name,
    )


# ---------------------------------------------------------------------------
# Embedded HTML
# ---------------------------------------------------------------------------
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PDF Translator</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 880px; margin: 32px auto; padding: 0 20px; color: #222; }
  h1 { margin-top: 0; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; margin: 12px 0; }
  label { display: flex; flex-direction: column; font-size: 13px; color: #555; gap: 4px; }
  select, input[type=file] { padding: 6px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
  button { background: #2563eb; color: #fff; border: 0; padding: 10px 18px;
           border-radius: 4px; font-size: 14px; cursor: pointer; }
  button:disabled { background: #94a3b8; cursor: not-allowed; }
  button.danger { background: #b91c1c; }
  button.danger:disabled { background: #94a3b8; }
  progress { width: 100%; height: 20px; }
  #log { height: 220px; overflow: auto; background: #0f172a; color: #a7f3d0;
         font-family: ui-monospace, Menlo, monospace; font-size: 12px;
         padding: 12px; border-radius: 4px; white-space: pre-wrap; }
  #downloads a { display: inline-block; margin: 4px 8px 4px 0; padding: 6px 10px;
                background: #f1f5f9; color: #0f172a; text-decoration: none;
                border-radius: 4px; font-size: 13px; }
  #downloads a:hover { background: #e2e8f0; }
  .status { font-size: 13px; color: #475569; }
  .error { color: #b91c1c; font-weight: 500; }
</style>
</head>
<body>
  <h1>PDF Translator</h1>
  <div class="row">
    <label>Model
      <select id="model">
        <option value="">loading...</option>
      </select>
    </label>
  </div>
  <div class="row">
    <label>Source language
      <select id="langIn">
        <option value="auto">auto (detect)</option>
        <option value="en" selected>English (en)</option>
        <option value="zh">Chinese (zh)</option>
        <option value="ja">Japanese (ja)</option>
        <option value="fr">French (fr)</option>
        <option value="de">German (de)</option>
        <option value="es">Spanish (es)</option>
        <option value="ru">Russian (ru)</option>
        <option value="ko">Korean (ko)</option>
        <option value="pt">Portuguese (pt)</option>
        <option value="it">Italian (it)</option>
      </select>
    </label>
    <label>Target language
      <select id="langOut">
        <option value="en">English (en)</option>
        <option value="zh" selected>Chinese (zh)</option>
        <option value="ja">Japanese (ja)</option>
        <option value="fr">French (fr)</option>
        <option value="de">German (de)</option>
        <option value="es">Spanish (es)</option>
        <option value="ru">Russian (ru)</option>
        <option value="ko">Korean (ko)</option>
        <option value="pt">Portuguese (pt)</option>
        <option value="it">Italian (it)</option>
      </select>
    </label>
  </div>
  <div class="row">
    <input type="file" id="file" accept="application/pdf">
    <button id="go">Translate</button>
    <button id="pause" style="display:none">Pause</button>
    <button id="resume" style="display:none;background:#059669">Resume</button>
    <button id="cancel" class="danger" style="display:none">Cancel</button>
  </div>
  <div class="row">
    <progress id="bar" value="0" max="100"></progress>
    <span id="pct" class="status">0%</span>
    <span id="elapsed" class="status" style="display:none"></span>
  </div>
  <pre id="log"></pre>
  <div id="downloads"></div>

<script>
const $ = (id) => document.getElementById(id);
const log = (m) => { const el = $("log"); el.textContent += m + "\\n"; el.scrollTop = el.scrollHeight; };
let elapsedTimer = null;
function startElapsed() {
  const t0 = Date.now();
  $("elapsed").style.display = "";
  elapsedTimer = setInterval(() => {
    const sec = Math.round((Date.now() - t0) / 1000);
    $("elapsed").textContent = sec < 60 ? sec + "s" : Math.floor(sec/60) + "m" + (sec%60) + "s";
  }, 1000);
}
function stopElapsed() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; } }

// Populate model dropdown from config.json on page load.
(async function loadModels() {
  try {
    const r = await fetch("/api/models");
    const d = await r.json();
    const sel = $("model");
    sel.innerHTML = '';
    (d.models || []).forEach(m => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m === d.default ? m + " (default)" : m;
      if (m === d.default) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (e) {
    $("model").innerHTML = '<option value="">(no models configured)</option>';
  }
})();

$("go").addEventListener("click", async () => {
  const file = $("file").files[0];
  if (!file) { alert("Choose a PDF first"); return; }
  $("go").disabled = true;
  $("cancel").style.display = "inline-block";
  $("cancel").disabled = false;
  $("pause").style.display = "inline-block";
  $("pause").disabled = false;
  $("resume").style.display = "none";
  $("downloads").innerHTML = "";
  $("log").textContent = "";
  $("bar").value = 0; $("pct").textContent = "0%";
  stopElapsed();
  const fd = new FormData();
  fd.append("file", file);
  fd.append("lang_in", $("langIn").value);
  fd.append("lang_out", $("langOut").value);
  const modelVal = $("model").value;
  if (modelVal) fd.append("model", modelVal);
  log("Uploading " + file.name + " (" + (file.size / 1024 / 1024).toFixed(1) + " MB) ...");
  let r;
  try {
    r = await fetch("/api/translate", { method: "POST", body: fd });
  } catch (e) {
    log("Upload error: " + e);
    finishClient();
    return;
  }
  if (!r.ok) { log("Error: " + r.status + " " + (await r.text())); finishClient(); return; }
  const { task_id } = await r.json();
  log("task_id = " + task_id);

  let currentEs = null;
  function connectSSE(tid) {
    if (currentEs) { currentEs.close(); currentEs = null; }
    stopElapsed();
    currentEs = new EventSource("/api/tasks/" + tid + "/events");
    currentEs.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === "started") {
      log("Translation started – " + (e.filename || "")
          + (e.model ? " | model=" + e.model : ""));
      startElapsed();
    } else if (e.type === "stage_summary") {
      // Show the pipeline stages with their estimated weight percentages.
      const stages = e.stages || [];
      if (stages.length) {
        const names = stages.map(s => s.name + " (~" + Math.round(s.percent * 100) + "%)").join(" → ");
        log("Pipeline: " + names);
      }
    } else if (e.type === "progress_start") {
      const extra = e.total_elapsed ? " | total=" + e.total_elapsed.toFixed(0) + "s" : "";
      log("▶ " + e.stage + " (0/" + e.stage_total + ")" + extra);
    } else if (e.type === "progress_update") {
      if (typeof e.overall_progress === "number") {
        $("bar").value = e.overall_progress;
        $("pct").textContent = e.overall_progress.toFixed(1) + "%";
      }
      let info = "  " + e.stage_current + "/" + e.stage_total;
      if (e.stage_elapsed) info += " | stage=" + e.stage_elapsed.toFixed(0) + "s";
      if (e.total_elapsed) info += " total=" + e.total_elapsed.toFixed(0) + "s";
      log(info);
    } else if (e.type === "progress_end") {
      const extra = e.total_elapsed ? " | total=" + e.total_elapsed.toFixed(0) + "s" : "";
      log("✔ " + e.stage + " done" + extra);
    } else if (e.type === "progress_stall") {
      log("⚠ stalled: no progress for " + e.elapsed_since_last_event.toFixed(0)
          + "s (stage: " + (e.current_stage || "?") + ")");
    } else if (e.type === "finish") {
      $("bar").value = 100; $("pct").textContent = "100%";
      log("Done in " + e.result.total_seconds.toFixed(1) + "s");
      const links = [
        ["mono", "Mono (translated)"],
        ["dual", "Dual (bilingual)"],
      ];
      const div = $("downloads");
      for (const [k, label] of links) {
        const a = document.createElement("a");
        a.href = "/api/tasks/" + task_id + "/download/" + k;
        a.textContent = "Download " + label;
        a.download = "";
        div.appendChild(a);
      }
      stopElapsed();
      if (currentEs) currentEs.close();
      finishClient();
    } else if (e.type === "paused") {
      log("⏸ Paused — resume or change model above and click Resume");
      $("pause").style.display = "none";
      $("cancel").style.display = "none";
      $("resume").style.display = "inline-block";
      $("resume").disabled = false;
      $("go").disabled = false;
      stopElapsed();
      if (currentEs) currentEs.close();
    } else if (e.type === "cancelled") {
      log("Cancelled by user");
      stopElapsed();
      if (currentEs) currentEs.close();
      finishClient();
    } else if (e.type === "error") {
      log("ERROR: " + e.error);
      stopElapsed();
      if (currentEs) currentEs.close();
      finishClient();
    } else if (e.type === "perf") {
      const stages = Object.entries(e.stages || {})
        .map(([k, v]) => k + "=" + v.toFixed(2) + "s")
        .join(", ");
      log("perf (" + e.reason + ", total=" + e.elapsed_total.toFixed(2) + "s): "
          + (stages || "(no stages)"));
    } else {
      log("event: " + e.type);
    }
  };
    currentEs.onerror = () => { log("SSE connection closed"); stopElapsed(); finishClient(); };
  }
  connectSSE(task_id);

  $("cancel").onclick = async () => {
    $("cancel").disabled = true;
    $("pause").disabled = true;
    log("Cancelling ...");
    try {
      await fetch("/api/tasks/" + task_id + "/cancel", { method: "POST" });
    } catch (e) { log("Cancel request failed: " + e); }
  };
  $("pause").onclick = async () => {
    $("pause").disabled = true;
    log("Pausing ...");
    try {
      await fetch("/api/tasks/" + task_id + "/pause", { method: "POST" });
    } catch (e) { log("Pause request failed: " + e); }
  };
  $("resume").onclick = async () => {
    $("resume").disabled = true;
    const newModel = $("model").value;
    log("Resuming with model=" + (newModel || "default") + " ...");
    try {
      const url = "/api/tasks/" + task_id + "/resume"
        + (newModel ? "?model=" + encodeURIComponent(newModel) : "");
      await fetch(url, { method: "POST" });
      $("pause").style.display = "inline-block";
      $("pause").disabled = false;
      $("resume").style.display = "none";
      $("cancel").style.display = "inline-block";
      $("cancel").disabled = false;
      $("bar").value = 0; $("pct").textContent = "0%";
      connectSSE(task_id);  // re-open SSE for resumed task
    } catch (e) { log("Resume request failed: " + e); }
  };
});

function finishClient() {
  $("go").disabled = false;
  $("pause").style.display = "none";
  $("pause").onclick = null;
  $("resume").style.display = "none";
  $("resume").onclick = null;
  $("cancel").style.display = "none";
  $("cancel").onclick = null;
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main():
    import signal

    import uvicorn

    cfg = load_config(CONFIG_PATH)  # fail fast on bad config
    log.info("active_model=%s, host=%s, port=%d", cfg.active_model, cfg.host, cfg.port)

    # Suppress the default SIGINT handler so Ctrl-C produces a clean exit
    # instead of a KeyboardInterrupt traceback.
    interrupted = False

    def _handle_sigint(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            log.info("Received Ctrl-C, shutting down gracefully ...")
        else:
            log.warning("Second Ctrl-C received, forcing exit")
            raise KeyboardInterrupt

    original_handler = signal.signal(signal.SIGINT, _handle_sigint)
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    except KeyboardInterrupt:
        log.info("Server stopped")
    finally:
        signal.signal(signal.SIGINT, original_handler)


if __name__ == "__main__":
    sys.exit(main() or 0)
