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
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# --- pdf2zh_next reuse -----------------------------------------------------
# Importing pdf2zh_next.main has the side effect of initialising the
# translation cache (peewee + SQLite at ~/.cache/pdf2zh_next/cache.v1.db)
# and logging configuration. Keep this import first.
import babeldoc.assets.assets  # noqa: F401  warmup side-effect
# Importing pdf2zh_next.translator.cache runs init_db() at module bottom
# (creates ~/.cache/pdf2zh_next/cache.v1.db and the translation-cache table).
import pdf2zh_next.translator.cache  # noqa: F401
from pdf2zh_next.config.translate_engine_model import ClaudeCodeSettings
from pdf2zh_next.config.translate_engine_model import OpenAISettings
from pdf2zh_next.translator.base_rate_limiter import BaseRateLimiter
from pdf2zh_next.translator.rate_limiter.qps_rate_limiter import QPSRateLimiter
from pdf2zh_next.translator.translator_impl.claudecode import ClaudeCodeTranslator
from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator

# BabelDoc
from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = APP_ROOT / "uploads"
OUTPUT_DIR = APP_ROOT / "outputs"
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
CONFIG_PATH = Path(os.environ.get("PDF2ZH_CONFIG", DEFAULT_CONFIG_PATH))

ALLOWED_MODELS = ("openai", "claudecode")
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
    )

    def __init__(self, task_id: str, pdf_path: Path, output_dir: Path):
        self.task_id = task_id
        self.pdf_path = pdf_path
        self.output_dir = output_dir
        self.status = "pending"   # pending / running / done / error / cancelled
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.result: dict | None = None
        self.error: str | None = None
        self.asyncio_task: asyncio.Task | None = None  # populated when worker starts


TASKS: dict[str, Task] = {}
TASK_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class AppConfig:
    """Holds validated translator settings derived from config.json."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.active_model: str = raw.get("active_model", "openai")
        if self.active_model not in ALLOWED_MODELS:
            raise ValueError(
                f"active_model must be one of {ALLOWED_MODELS}, got {self.active_model!r}"
            )
        # Validate engine settings via Pydantic
        if self.active_model == "openai":
            self.engine_settings = OpenAISettings(**raw["openai"])
        else:
            self.engine_settings = ClaudeCodeSettings(**raw["claudecode"])
        self.engine_settings.validate_settings()
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

    def build_settings(self, lang_in: str, lang_out: str) -> SimpleNamespace:
        """Compose a minimal `settings`-like object the translators expect.

        OpenAITranslator and ClaudeCodeTranslator read:
          - settings.translate_engine_settings.<engine_specific_fields>
          - settings.translation.{lang_in, lang_out, ignore_cache}
        """
        return SimpleNamespace(
            translate_engine_settings=self.engine_settings,
            translation=SimpleNamespace(
                lang_in=lang_in,
                lang_out=lang_out,
                ignore_cache=self.ignore_cache,
                qps=self.qps,
            ),
        )


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
def make_translator(cfg: AppConfig, settings: SimpleNamespace) -> Any:
    rate_limiter: BaseRateLimiter = QPSRateLimiter(cfg.qps) if cfg.qps > 0 else BaseRateLimiter()
    if cfg.active_model == "openai":
        return OpenAITranslator(settings, rate_limiter)
    return ClaudeCodeTranslator(settings, rate_limiter)


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


async def run_translation(task: Task, lang_in: str, lang_out: str) -> None:
    cfg = await get_config()
    settings = cfg.build_settings(lang_in, lang_out)
    translator = make_translator(cfg, settings)
    term_translator = make_translator(cfg, settings)  # same engine for now

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
    task.status = "running"

    fm = _file_meta(task.pdf_path)
    log.info(
        "[%s] translation start | engine=%s | model=%s | %s→%s | qps=%d | "
        "watermark=%s | no_dual=%s | no_mono=%s | file=%s (%.2f MB)",
        task.task_id,
        cfg.active_model,
        getattr(cfg.engine_settings,
                "openai_model" if cfg.active_model == "openai" else "claude_code_model",
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
    run_t0 = time.monotonic()

    async def emit(event: dict) -> None:
        await task.events.put(event)

    async def emit_perf(reason: str) -> None:
        await emit({
            "type": "perf",
            "reason": reason,
            "elapsed_total": round(time.monotonic() - run_t0, 3),
            "stages": {k: round(v, 3) for k, v in perf.items()},
        })

    try:
        async for event in async_translate(config):
            etype = event.get("type")

            # ----- stage timing --------------------------------------------
            if etype == "progress_start":
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
                    log.info(
                        "[%s]   stage START %s (total=%s)",
                        task.task_id, current_stage, stage_total,
                    )
            elif etype == "progress_end":
                if current_stage is not None and current_stage_t0 is not None:
                    elapsed = time.monotonic() - current_stage_t0
                    perf[current_stage] = perf.get(current_stage, 0.0) + elapsed
                    log.info(
                        "[%s]   stage END  %s (%.2fs, items=%s)",
                        task.task_id, current_stage, elapsed,
                        event.get("stage_total"),
                    )
            elif etype == "progress_update":
                # Throttle: log only on integer-percent transitions, not every
                # progress event (those come at report_interval cadence).
                pct = event.get("overall_progress")
                if isinstance(pct, (int, float)):
                    int_pct = int(pct)
                    if int_pct != last_progress_pct and int_pct % 5 == 0:
                        log.info(
                            "[%s]     progress %s: %d%% (%d/%d) | stage=%s",
                            task.task_id,
                            current_stage or "?",
                            int_pct,
                            event.get("stage_current", 0),
                            event.get("stage_total", 0),
                            event.get("stage"),
                        )
                        last_progress_pct = int_pct

            # ----- fan-out to SSE ------------------------------------------
            event_out = {k: v for k, v in event.items() if k != "translate_result"}
            if etype == "finish":
                res = event["translate_result"]
                task.result = {
                    "original_pdf_path": str(getattr(res, "original_pdf_path", "")),
                    "mono_pdf_path": str(getattr(res, "mono_pdf_path", "") or ""),
                    "dual_pdf_path": str(getattr(res, "dual_pdf_path", "") or ""),
                    "no_watermark_mono_pdf_path": str(
                        getattr(res, "no_watermark_mono_pdf_path", "") or ""
                    ),
                    "no_watermark_dual_pdf_path": str(
                        getattr(res, "no_watermark_dual_pdf_path", "") or ""
                    ),
                    "auto_extracted_glossary_path": str(
                        getattr(res, "auto_extracted_glossary_path", "") or ""
                    ),
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
        log.warning(
            "[%s] translation CANCELLED after %.2fs | partial_stages=%s",
            task.task_id, time.monotonic() - run_t0,
            {k: round(v, 2) for k, v in perf.items()},
        )
        task.status = "cancelled"
        await emit_perf("cancelled")
        await emit({"type": "cancelled", "task_id": task.task_id})
        raise
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
        await task.events.put(None)  # sentinel


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    await get_config()
    yield


app = FastAPI(title="PDF Translator", lifespan=lifespan)


@app.get("/health")
async def health():
    cfg = await get_config()
    return {"status": "ok", "active_model": cfg.active_model}


@app.post("/api/reload-config")
async def api_reload_config():
    cfg = await reload_config()
    return {"ok": True, "active_model": cfg.active_model}


@app.post("/api/translate")
async def api_translate(
    file: UploadFile = File(...),
    lang_in: str = Form(...),
    lang_out: str = Form(...),
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

    task_id = uuid.uuid4().hex
    pdf_path = UPLOAD_DIR / f"{task_id}.pdf"
    out_dir = OUTPUT_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with pdf_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    log.info("Saved upload %s -> %s", file.filename, pdf_path)

    task = Task(task_id, pdf_path, out_dir)
    async with TASK_LOCK:
        TASKS[task_id] = task
        task.asyncio_task = asyncio.create_task(run_translation(task, lang_in, lang_out))
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
    """Cancel a running translation task.

    Idempotent: returns 200 even if the task is already finished/cancelled.
    """
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in ("done", "error", "cancelled"):
        return {"task_id": task_id, "status": task.status, "already_finished": True}
    if task.asyncio_task and not task.asyncio_task.done():
        task.asyncio_task.cancel()
        log.info("Cancel requested for task %s", task_id)
    return {"task_id": task_id, "status": "cancelling"}


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
    "dual_no_watermark": "no_watermark_dual_pdf_path",
    "mono_no_watermark": "no_watermark_mono_pdf_path",
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
  <p class="status">Defaults: source <b>English</b> → target <b>Chinese</b>.</p>
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
    <button id="cancel" class="danger" style="display:none">Cancel</button>
  </div>
  <div class="row">
    <progress id="bar" value="0" max="100"></progress>
    <span id="pct" class="status">0%</span>
    <span id="stage" class="status"></span>
  </div>
  <pre id="log"></pre>
  <div id="downloads"></div>

<script>
const $ = (id) => document.getElementById(id);
const log = (m) => { const el = $("log"); el.textContent += m + "\\n"; el.scrollTop = el.scrollHeight; };

$("go").addEventListener("click", async () => {
  const file = $("file").files[0];
  if (!file) { alert("Choose a PDF first"); return; }
  $("go").disabled = true;
  $("cancel").style.display = "inline-block";
  $("cancel").disabled = false;
  $("downloads").innerHTML = "";
  $("log").textContent = "";
  $("bar").value = 0; $("pct").textContent = "0%";
  const fd = new FormData();
  fd.append("file", file);
  fd.append("lang_in", $("langIn").value);
  fd.append("lang_out", $("langOut").value);
  log("Uploading " + file.name + " ...");
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

  $("cancel").onclick = async () => {
    $("cancel").disabled = true;
    log("Cancelling ...");
    try {
      await fetch("/api/tasks/" + task_id + "/cancel", { method: "POST" });
    } catch (e) { log("Cancel request failed: " + e); }
  };

  const es = new EventSource("/api/tasks/" + task_id + "/events");
  es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === "progress_start") {
      $("stage").textContent = e.stage + " (0/" + e.stage_total + ")";
    } else if (e.type === "progress_update") {
      if (typeof e.overall_progress === "number") {
        $("bar").value = e.overall_progress;
        $("pct").textContent = e.overall_progress.toFixed(1) + "%";
      }
      $("stage").textContent = e.stage + " (" + e.stage_current + "/" + e.stage_total + ")";
    } else if (e.type === "progress_end") {
      $("stage").textContent = e.stage + " done";
    } else if (e.type === "finish") {
      $("bar").value = 100; $("pct").textContent = "100%";
      log("Done in " + e.result.total_seconds.toFixed(1) + "s");
      const links = [
        ["dual", "Dual (bilingual)"],
        ["mono", "Mono (translated only)"],
        ["dual_no_watermark", "Dual (no watermark)"],
        ["mono_no_watermark", "Mono (no watermark)"],
      ];
      const div = $("downloads");
      for (const [k, label] of links) {
        const a = document.createElement("a");
        a.href = "/api/tasks/" + task_id + "/download/" + k;
        a.textContent = "Download " + label;
        a.download = "";
        div.appendChild(a);
      }
      es.close();
      finishClient();
    } else if (e.type === "cancelled") {
      log("Cancelled by user");
      es.close();
      finishClient();
    } else if (e.type === "error") {
      log("ERROR: " + e.error);
      es.close();
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
  es.onerror = () => { log("SSE connection closed"); finishClient(); };
});

function finishClient() {
  $("go").disabled = false;
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
    import uvicorn
    cfg = load_config(CONFIG_PATH)  # fail fast on bad config
    log.info("active_model=%s, host=%s, port=%d", cfg.active_model, cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main() or 0)
