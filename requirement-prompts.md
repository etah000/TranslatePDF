# requirement-prompts.md

> A user-style prompt pack for handing this project to an AI code-generation
> agent (Claude / GPT-4-class). Each section is a self-contained prompt you
> can paste into a chat. The "as-built" version of every prompt is what
> landed in this repo (`serve.py`, `config.json`, `USER_GUIDE.md`,
> `PLAN.md`); divergences are noted inline so the prompts remain a useful
> starting point for the next round.

---

## 0. How to use this file

- **Each `Prompt N` block below is a complete, paste-ready user message.**
- They are ordered to match a real implementation order: spec → scaffold →
  routes → SSE → UI → config → polish.
- If you want the AI to do the whole thing in one go, paste **Prompt 0** +
  the constraints from §1; otherwise feed them in order.
- All prompts assume the AI has filesystem write access to the repo root.

---

## Prompt 0 — one-shot: build the whole thing

> I'm working in a repo at `/data/opensource/PDFMathTranslate-next` that
> already wraps BabelDOC for PDF translation. BabelDOC does the actual
> PDF-parsing-and-rendering work; the repo's own code is a productisation
> layer (Gradio UI, 24 LLM adapters, pydantic SettingsModel, subprocess
> isolation, 8-language docs, Windows EXE packaging, etc.). I want to
> replace that whole productisation layer with a single-file FastAPI
> service and a tiny HTML page.
>
> Please do the following:
>
> 1. Create `config.json` at the repo root with the structure described in
>    `USER_GUIDE.md` §3. Default `active_model` = `"openai"`, defaults
>    `lang_in` = `en`, `lang_out` = `zh`, `qps` = 4, `watermark_output_mode`
>    = `"no watermark"`. Pre-fill `openai.openai_api_key` with a
>    placeholder string and a comment-style note that the user must
>    replace it.
>
> 2. Create `serve.py` at the repo root — a single-file FastAPI app
>    (~300–400 lines) with:
>    - `GET /` returning an HTML page inlined as a Python triple-quoted
>      string.
>    - `GET /health` returning `{"status":"ok","active_model":...}`.
>    - `POST /api/reload-config` re-reading `config.json`.
>    - `POST /api/translate` accepting multipart upload (`file`, `lang_in`,
>      `lang_out`) and returning `{"task_id": ...}`.
>    - `GET /api/tasks/{id}` returning status / result / error.
>    - `GET /api/tasks/{id}/events` returning a Server-Sent-Events stream
>      of BabelDOC's progress events verbatim.
>    - `POST /api/tasks/{id}/cancel` cancelling the asyncio task backing
>      the translation.
>    - `GET /api/tasks/{id}/download/{kind}` returning the translated PDF
>      (`kind` ∈ `dual` / `mono` / `dual_no_watermark` /
>      `mono_no_watermark`).
>
> 3. The HTML page must contain only:
>    - a file input accepting `application/pdf`,
>    - a `Source language` `<select>` with `auto / en / zh / ja / fr / de /
>      es / ru / ko / pt / it` (default `en`),
>    - a `Target language` `<select>` with the same 10 codes **minus**
>      `auto` (default `zh`),
>    - a `Translate` button (disabled while running),
>    - a red `Cancel` button (hidden until a task is running),
>    - a `<progress>` bar + percentage label,
>    - a `<pre>` log area streaming events,
>    - a download-links area populated on `finish`.
>
> 4. **Reuse, do not re-implement:**
>    - `pdf2zh_next.translator.translator_impl.openai.OpenAITranslator`
>    - `pdf2zh_next.translator.translator_impl.claudecode.ClaudeCodeTranslator`
>    - `pdf2zh_next.translator.rate_limiter.qps_rate_limiter.QPSRateLimiter`
>    - `pdf2zh_next.translator.cache.init_db` (already runs at import via
>      `pdf2zh_next.main`)
>    - `pdf2zh_next.config.translate_engine_model.{OpenAISettings,
>      ClaudeCodeSettings}` for Pydantic validation of the engine block
>    - `babeldoc.format.pdf.high_level.async_translate`
>    - `babeldoc.format.pdf.translation_config.{TranslationConfig,
>      WatermarkOutputMode}`
>
> 5. **Do not introduce:**
>    - Gradio or any other UI framework
>    - The `pdf2zh_next.config.model.SettingsModel` Pydantic model — wire
>      per-request `lang_in` / `lang_out` directly into a `SimpleNamespace`
>      that the existing translators accept
>    - The `multiprocessing.Process` wrapper from
>      `pdf2zh_next.high_level._translate_in_subprocess` — run the
>      translation as an `asyncio.create_task` in the FastAPI event loop
>    - The `[project.scripts]` console-script entries `pdf2zh` /
>      `pdf2zh2` / `pdf2zh_next` in `pyproject.toml`
>
> 6. Hard-coded constraints:
>    - The HTML form's target language defaults to `zh`; the service
>      rejects any `lang_out` not in the 10-code whitelist with HTTP 422.
>    - `auto_extract_glossary=False` is fixed in the
>      `TranslationConfig(...)` constructor inside `run_translation()`.
>    - Output PDFs in `./outputs/{task_id}/` are kept **forever**; no
>      cleanup job.
>
> 7. Logging:
>    - At startup, raise 20 BabelDoc internal loggers (`babeldoc`,
>      `babeldoc.format.pdf.document_il.midend.layout_parser`,
>      `il_translator`, `paragraph_finder`,
>      `automatic_term_extractor`, `pdf_creater`, …) from WARNING to INFO
>      so users can see what's happening.
>    - In `run_translation`, log a `translation start` line with
>      engine/model/languages/qps/file-size; for every BabelDOC stage
>      start log `stage START <name> (total=N)`; for every end log
>      `stage END <name> (Xs, items=N)`. Log integer-percent
>      `progress` lines throttled to every 5%. Log a final `stage
>      breakdown` line. Same on cancel (`translation CANCELLED` at
>      WARNING) and on error (`translation FAILED` at ERROR with
>      traceback).
>    - Emit a `perf` SSE event on done / cancel / error with
>      `{elapsed_total, stages: {name: seconds}}` so the UI can show it.
>
> 8. Update `pyproject.toml` to:
>    - add `python-multipart>=0.0.9` to `dependencies`
>      (fastapi/uvicorn are already there),
>    - **delete** the entire `[project.scripts]` block (the three
>      `pdf2zh*` entries).
>
> 9. Verify with:
>    - `python -c "import serve"` (must succeed; it triggers
>      `init_db()` via the `pdf2zh_next.main` import),
>    - `python -c "import serve; cfg = serve.load_config(serve.CONFIG_PATH);
>      s = cfg.build_settings('en', 'zh'); tr = serve.make_translator(cfg,
>      s); print(tr.name)"` (must print `openai`),
>    - `uvicorn serve:app --port 8765` then `curl /health`,
>      `curl /openapi.json` (7 routes), `curl -X POST /api/reload-config`,
>      and the negative cases (non-PDF upload, invalid `lang_out`).
>
> Do not write tests, do not add documentation beyond what's strictly
> necessary, and keep `pdf2zh_next/` source files untouched.

---

## Prompt 1 — the project spec (paste this first if you want the AI to plan)

> Build a small FastAPI service that translates PDF files using BabelDOC.
> The user uploads a PDF, picks source and target languages from
> dropdowns, and watches real-time progress until the translated PDFs
> are ready to download.
>
> Constraints — please treat these as hard requirements:
>
> 1. **Engine choices: exactly two.** `openai` and `claudecode`. Anything
>    else is rejected with HTTP 422.
>
> 2. **Languages: a fixed whitelist of 10 codes** for both source and
>    target — `en`, `zh`, `ja`, `fr`, `de`, `es`, `ru`, `ko`, `pt`, `it`.
>    Source may additionally be `auto`. Target may NOT be `auto`. Source
>    default = `en`, target default = `zh`. Any other code → 422.
>
> 3. **Configuration is one JSON file** (`config.json`) with the
>    following top-level keys: `active_model`, `openai`, `claudecode`,
>    `translation`, `server`. The file is read once at startup, but
>    `POST /api/reload-config` re-reads it on demand.
>
> 4. **Do not reimplement LLM client code.** Reuse
>    `pdf2zh_next.translator.translator_impl.openai.OpenAITranslator` and
>    `pdf2zh_next.translator.translator_impl.claudecode.ClaudeCodeTranslator`
>    as-is, plus their `QPSRateLimiter` and the SQLite translation cache
>    from `pdf2zh_next.translator.cache`.
>
> 5. **Do not bring in the upstream productisation layer.** No Gradio, no
>    `pdf2zh_next.config.model.SettingsModel`, no
>    `_translate_in_subprocess` wrapper, no `pyproject.toml` console
>    scripts. The whole service must be one Python file at the repo root.
>
> 6. **The HTML UI must be a single page served by FastAPI**, with no
>    static asset files, no build step, no JS framework. Vanilla JS only.
>    Must include: file input, source/target language dropdowns,
>    Translate button, Cancel button (hidden until a task is running),
>    progress bar, scrolling log area, and download-link area.
>
> 7. **Default-disable term extraction** (BabelDOC's
>    `auto_extract_glossary`) — it's slow and rarely worth it.
>
> 8. **Per-stage timing logs** for every BabelDOC stage, plus a `perf`
>    SSE event on done/cancel/error that ships a stage-by-stage elapsed
>    breakdown to the UI.
>
> 9. **Cancellation** is via `POST /api/tasks/{id}/cancel` and must
>    propagate into BabelDOC's internal `cancel_event`; the SSE stream
>    must end with a `cancelled` event.
>
> 10. **Translated PDFs land in `./outputs/{task_id}/`** and stay there
>     forever — no auto-cleanup. Originals land in `./uploads/{task_id}.pdf`
>     and likewise stay forever.
>
> 11. **Output file kinds:** `dual` (bilingual) and `mono` (translated
>     only), each in a watermarked and a no-watermark variant — up to 4
>     files per task, depending on `no_dual` / `no_mono` /
>     `watermark_output_mode` in `config.json`.
>
> 12. **Acceptance:**
>     - `python -c "import serve"` works.
>     - `GET /health` returns `{"status":"ok","active_model":"openai"}`.
>     - Uploading a non-PDF returns 422 with `{"detail":"file must be a PDF"}`.
>     - `lang_out=de` returns 422 with `{"detail":"lang_out 'de' not in [...]"}`.
>     - `GET /openapi.json` lists 8 routes.

---

## Prompt 2 — config file

> Write `config.json` at the repo root. The file is the single source of
> truth for engine selection, LLM parameters, translation behaviour, and
> the HTTP bind address. Schema:
>
> ```json
> {
>   "active_model": "openai",
>   "openai": {
>     "translate_engine_type": "OpenAI",
>     "openai_model": "gpt-4o-mini",
>     "openai_base_url": "https://api.openai.com/v1",
>     "openai_api_key": "sk-REPLACE-ME",
>     "openai_timeout": "60",
>     "openai_temperature": "0",
>     "openai_reasoning_effort": null,
>     "openai_enable_json_mode": false,
>     "openai_send_temprature": false,
>     "openai_send_reasoning_effort": false
>   },
>   "claudecode": {
>     "translate_engine_type": "ClaudeCode",
>     "claude_code_path": "claude",
>     "claude_code_model": "sonnet"
>   },
>   "translation": {
>     "qps": 4,
>     "ignore_cache": false,
>     "no_dual": false,
>     "no_mono": false,
>     "watermark_output_mode": "no watermark",
>     "min_text_length": 5
>   },
>   "server": { "host": "0.0.0.0", "port": 8765 }
> }
> ```
>
> Two important gotchas — please get these right:
>
> - `openai.translate_engine_type` must be the literal string `"OpenAI"`
>   (it's the engine class identifier, not the model name). If the user
>   wants to talk to MiniMax, DeepSeek, vLLM, etc., they keep
>   `translate_engine_type="OpenAI"` and change `openai_model` and
>   `openai_base_url` to point at that provider. The Pydantic model
>   `OpenAISettings` has a `Literal["OpenAI"]` discriminator and will
>   reject any other value.
> - `openai.openai_timeout` is read as a string by Pydantic — use
>   `"60"`, not `60`.
>
> `watermark_output_mode` accepts the variants `watermarked` /
> `no_watermark` / `both` / `no watermark` (with space) / `nowatermark`
> — the server normalises them all to the BabelDOC enum.

---

## Prompt 3 — `serve.py` scaffold + config loader

> Create `serve.py` at the repo root. It must:
>
> 1. Import `pdf2zh_next.main` at the top — this has the side effect of
>    initialising the SQLite translation cache at
>    `~/.cache/pdf2zh_next/cache.v1.db`. Don't try to call `init_db()`
>    yourself.
>
> 2. Define these module-level constants:
>    ```python
>    APP_ROOT = Path(__file__).resolve().parent
>    UPLOAD_DIR = APP_ROOT / "uploads"
>    OUTPUT_DIR = APP_ROOT / "outputs"
>    DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
>    CONFIG_PATH = Path(os.environ.get("PDF2ZH_CONFIG", DEFAULT_CONFIG_PATH))
>    ALLOWED_MODELS = ("openai", "claudecode")
>    DEFAULT_SOURCE_LANG = "en"
>    DEFAULT_TARGET_LANG = "zh"
>    ALLOWED_SOURCE_LANGS = ("auto", "en", "zh", "ja", "fr", "de", "es", "ru", "ko", "pt", "it")
>    ALLOWED_TARGET_LANGS = ("en", "zh", "ja", "fr", "de", "es", "ru", "ko", "pt", "it")
>    WATERMARK_MAP = {
>        "watermarked":   WatermarkOutputMode.Watermarked,
>        "no_watermark":  WatermarkOutputMode.NoWatermark,
>        "no watermark":  WatermarkOutputMode.NoWatermark,
>        "nowatermark":   WatermarkOutputMode.NoWatermark,
>        "both":          WatermarkOutputMode.Both,
>    }
>    ```
>
> 3. Implement an `AppConfig` class that:
>    - Loads JSON from a path,
>    - Validates `active_model` against `ALLOWED_MODELS`,
>    - Constructs the appropriate `OpenAISettings(**raw["openai"])` or
>      `ClaudeCodeSettings(**raw["claudecode"])` from
>      `pdf2zh_next.config.translate_engine_model` and calls
>      `.validate_settings()`,
>    - Stores `qps`, `ignore_cache`, `no_dual`, `no_mono`,
>      `watermark_str`, `min_text_length`, `host`, `port`,
>    - Exposes a `build_settings(lang_in, lang_out)` method that returns
>      a `SimpleNamespace(translate_engine_settings=<the Pydantic model>,
>      translation=SimpleNamespace(lang_in, lang_out, ignore_cache,
>      qps))`. The existing translators read those two attribute paths and
>      nothing else.
>
> 4. Provide `load_config(path) -> AppConfig` and an async
>    `get_config()` / `reload_config()` pair guarded by an
>    `asyncio.Lock`.
>
> 5. Implement `make_translator(cfg, settings)` that returns
>    `OpenAITranslator(settings, rate_limiter)` or
>    `ClaudeCodeTranslator(settings, rate_limiter)` per `cfg.active_model`,
>    with `QPSRateLimiter(cfg.qps)` (or `BaseRateLimiter()` if
>    `qps <= 0`).
>
> 6. Create a FastAPI app with a `lifespan` context that ensures
>    `UPLOAD_DIR` and `OUTPUT_DIR` exist and primes the config. Mount
>    routes (defined in next prompts).
>
> 7. Provide a `main()` that calls `uvicorn.run(app, host=cfg.host,
>    port=cfg.port)` for `python serve.py` invocations.
>
> Do not add the routes yet; this prompt is just the scaffold.

---

## Prompt 4 — translation worker

> Implement the async worker `run_translation(task, lang_in, lang_out)`
> in `serve.py`:
>
> 1. Build the engine via `make_translator`; build a per-request
>    `SimpleNamespace` settings via `cfg.build_settings(lang_in, lang_out)`.
> 2. Construct a `TranslationConfig(...)` from BabelDOC. Important
>    parameters:
>    - `input_file=task.pdf_path`
>    - `output_dir=task.output_dir`
>    - `translator=translator` and
>      `term_extraction_translator=<same translator>`
>    - `lang_in=lang_in`, `lang_out=lang_out`
>    - `doc_layout_model=None`
>    - `qps=cfg.qps`, `pool_max_workers=cfg.qps`
>    - `watermark_output_mode=WATERMARK_MAP[cfg.watermark_str]`
>    - `min_text_length=cfg.min_text_length`
>    - `report_interval=0.1`
>    - **`auto_extract_glossary=False`** (do not omit this)
> 3. Iterate `async for event in async_translate(config):` and:
>    - On `progress_start` / `progress_end` track per-stage elapsed time
>      keyed by `event["stage"]`, and log a `stage START` / `stage END`
>      line that includes the total item count and the elapsed seconds.
>    - On `progress_update` log an integer-percent line at most every 5%
>      (`int_pct % 5 == 0` and `int_pct != last_logged`).
>    - On `finish` populate `task.result` with a flat dict of strings
>      (paths and `total_seconds`), set `task.status = "done"`, log a
>      `translation DONE in Xs | babeldoc_reported=Ys` line, log a
>      `stage breakdown: name=Xs, name=Ys, ...` line, and emit a `perf`
>      SSE event with `{elapsed_total, stages: {name: secs}}`.
>    - Forward every event to the SSE queue as `{k: v for k, v in
>      event.items() if k != "translate_result"}` plus
>      `event_out["result"] = task.result` on `finish`.
> 4. On `asyncio.CancelledError`:
>    - Close any open stage's timer,
>    - Log a WARNING `translation CANCELLED after Xs | partial_stages=...`,
>    - Set `task.status = "cancelled"`,
>    - Emit `perf` (reason=`"cancelled"`) then
>      `{"type":"cancelled","task_id":...}` to the SSE queue,
>    - Re-raise.
> 5. On any other exception:
>    - Log with `log.exception("translation FAILED after Xs in stage=%r",
>      current_stage, exc)`,
>    - Set `task.status = "error"`, `task.error = str(exc)`,
>    - Emit `perf` (reason=`"error"`) then
>      `{"type":"error","error": str(exc)}`.
> 6. Always `await task.events.put(None)` as a sentinel at the end.
>
> Also bump the following BabelDoc loggers from WARNING to INFO at
> import time so the messages from steps 3 and 5 actually show up:
>
> ```
> babeldoc
> babeldoc.format.pdf.high_level
> babeldoc.format.pdf.document_il
> babeldoc.format.pdf.document_il.midend
> babeldoc.format.pdf.document_il.midend.il_translator
> babeldoc.format.pdf.document_il.midend.il_translator_llm_only
> babeldoc.format.pdf.document_il.midend.layout_parser
> babeldoc.format.pdf.document_il.midend.paragraph_finder
> babeldoc.format.pdf.document_il.midend.automatic_term_extractor
> babeldoc.format.pdf.document_il.midend.detect_scanned_file
> babeldoc.format.pdf.document_il.midend.styles_and_formulas
> babeldoc.format.pdf.document_il.midend.table_parser
> babeldoc.format.pdf.document_il.midend.typesetting
> babeldoc.format.pdf.document_il.backend
> babeldoc.format.pdf.document_il.backend.pdf_creater
> babeldoc.format.pdf.new_parser
> babeldoc.translator
> babeldoc.translator.translator
> pdf2zh_next.translator
> ```
>
> Store the `asyncio.Task` returned by `asyncio.create_task(...)` in a
> new `Task.asyncio_task` slot — the cancel endpoint needs it.

---

## Prompt 5 — HTTP routes

> Add the following routes to the FastAPI app in `serve.py`. The
> `Task` class has slots: `task_id`, `pdf_path`, `output_dir`, `status`,
> `events` (an `asyncio.Queue[dict]`), `result`, `error`,
> `asyncio_task`.
>
> - `GET /health` → `{"status":"ok","active_model": await get_config().active_model}`
> - `POST /api/reload-config` → `await reload_config()` and return
>   `{"ok": True, "active_model": ...}`.
> - `POST /api/translate(file, lang_in, lang_out)`:
>   - 422 with `{"detail":"file must be a PDF"}` if
>     `not file.filename.lower().endswith(".pdf")`.
>   - 422 with `{"detail":"lang_in 'X' not in [...]"}` if `lang_in`
>     not in `ALLOWED_SOURCE_LANGS`.
>   - 422 with `{"detail":"lang_out 'X' not in [...]"}` if `lang_out`
>     not in `ALLOWED_TARGET_LANGS`.
>   - Save the upload to `UPLOAD_DIR/{uuid4.hex}.pdf`.
>   - Create `OUTPUT_DIR/{uuid4.hex}/`.
>   - Insert a `Task` into the global `TASKS` dict, store the
>     `asyncio.Task` handle on it, and return `{"task_id": uuid4.hex}`.
> - `GET /api/tasks/{task_id}` → status / result / error from the
>   `Task` (404 if missing).
> - `GET /api/tasks/{task_id}/events` → `StreamingResponse` with
>   `media_type="text/event-stream"`. Loop reading
>   `await task.events.get()` with a 15-second timeout that yields
>   `: keepalive\n\n` keepalive comments. When the sentinel `None`
>   arrives, break. If the client disconnects (`request.is_disconnected()`),
>   break. Format each event as
>   `data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n`.
>   Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
> - `POST /api/tasks/{task_id}/cancel`:
>   - 404 if the task doesn't exist.
>   - If the task is already `done` / `error` / `cancelled`, return
>     `{"task_id":..., "status":..., "already_finished": True}`.
>   - Otherwise call `task.asyncio_task.cancel()` and return
>     `{"task_id":..., "status":"cancelling"}`.
> - `GET /api/tasks/{task_id}/download/{kind}`:
>   - 404 if missing, 409 if not yet finished, 422 if `kind` is not one
>     of `dual` / `mono` / `dual_no_watermark` / `mono_no_watermark`,
>     404 if the resulting path doesn't exist on disk.
>   - Otherwise return `FileResponse(path, media_type="application/pdf",
>     filename=Path(path).name)`.
> - `GET /` → `HTMLResponse(INDEX_HTML)` (the index string is added by
>   the next prompt).

---

## Prompt 6 — the HTML page

> Add a module-level `INDEX_HTML = """..."""` triple-quoted string in
> `serve.py` and mount it at `GET /`. It must be a single page with no
> external assets. Required structure:
>
> ```html
> <!doctype html>
> <html lang="en">
> <head>
>   <meta charset="utf-8">
>   <title>PDF Translator</title>
>   <style>/* inline CSS, ~30 lines */</style>
> </head>
> <body>
>   <h1>PDF Translator</h1>
>   <p class="status">Defaults: source <b>English</b> → target <b>Chinese</b>.</p>
>   <div class="row">
>     <label>Source language
>       <select id="langIn">
>         <option value="auto">auto (detect)</option>
>         <option value="en" selected>English (en)</option>
>         <!-- zh, ja, fr, de, es, ru, ko, pt, it -->
>       </select>
>     </label>
>     <label>Target language
>       <select id="langOut">
>         <option value="en">English (en)</option>
>         <option value="zh" selected>Chinese (zh)</option>
>         <!-- ja, fr, de, es, ru, ko, pt, it -->
>       </select>
>     </label>
>   </div>
>   <div class="row">
>     <input type="file" id="file" accept="application/pdf">
>     <button id="go">Translate</button>
>     <button id="cancel" class="danger" style="display:none">Cancel</button>
>   </div>
>   <div class="row">
>     <progress id="bar" value="0" max="100"></progress>
>     <span id="pct">0%</span>
>     <span id="stage"></span>
>   </div>
>   <pre id="log"></pre>
>   <div id="downloads"></div>
>   <script>/* vanilla JS — see below */</script>
> </body>
> </html>
> ```
>
> Required CSS classes: `.row` (flex), `.status`, button (blue) +
> `.danger` (red) + `:disabled` (grey), `progress` (full width), `#log`
> (220px tall, dark bg, monospace, auto-scroll), `#downloads a`
> (chip-style download links).
>
> Required JS behaviour:
>
> 1. On `#go` click:
>    - Disable `#go`, show `#cancel`, clear `#downloads`, reset bar and
>      log.
>    - `fetch('/api/translate', {method: 'POST', body: <FormData with
>      file/lang_in/lang_out>})`. On any error, log and call
>      `finishClient()`.
>    - On success, attach a click handler to `#cancel` that POSTs to
>      `/api/tasks/{id}/cancel` (and disables itself).
>    - Open `new EventSource('/api/tasks/{id}/events')`.
>
> 2. In `es.onmessage`, parse JSON and switch on `event.type`:
>    - `progress_start` → show `stage` and `0/total`.
>    - `progress_update` → update bar from `overall_progress`, update
>      `pct` text, show `stage current/total`.
>    - `progress_end` → show "stage done".
>    - `finish` → set bar to 100, log `Done in Xs`, append four
>      `<a download>` chips for `dual`, `mono`,
>      `dual_no_watermark`, `mono_no_watermark`, then `es.close()` +
>      `finishClient()`.
>    - `cancelled` → log "Cancelled by user", `es.close()`,
>      `finishClient()`.
>    - `error` → log `ERROR: <error>`, `es.close()`, `finishClient()`.
>    - `perf` → log
>      `perf (<reason>, total=<Xs>): name1=1.20s, name2=0.30s, ...`
>      built from `e.stages`.
>    - any other → log `event: <type>`.
>
> 3. `es.onerror` → log "SSE connection closed" and `finishClient()`.
>
> 4. `finishClient()` re-enables `#go`, hides `#cancel`, clears the
>    cancel click handler.

---

## Prompt 7 — `pyproject.toml` changes

> Modify `pyproject.toml`:
>
> 1. Add `"python-multipart>=0.0.9"` to the `dependencies` list
>    (fastapi/uvicorn are already listed).
> 2. Delete the entire `[project.scripts]` block — there should be no
>    `pdf2zh = "pdf2zh_next.main:cli"` (or sibling) entries left.
>
> Do not change the version, do not touch `[tool.ruff]`, do not remove
> existing dependencies.

---

## Prompt 8 — write the user guide

> Write `USER_GUIDE.md` at the repo root. It must have these top-level
> sections, in this order:
>
> 1. **Quick start** — three subsections (Prerequisites, Install, Edit
>    `config.json`, Start the service, Open the UI) with copy-pasteable
>    shell commands.
> 2. **How to use the app** — a numbered walkthrough of the UI.
> 3. **Configuration reference (`config.json`)** — five subsections
>    (Top-level, OpenAI engine, ClaudeCode engine, Translation behaviour,
>    HTTP server). Each parameter in a table with columns `Key | Type |
>    Default | Meaning`. **Important caveat to surface:** setting
>    `openai.translate_engine_type` to anything other than the literal
>    string `"OpenAI"` is a config error — that field identifies the
>    engine class, not the model.
> 4. **Per-request options (UI)** — table of source/target language
>    codes and their defaults.
> 5. **Cancel a running task** — explain what Cancel does, mention
>    idempotency.
> 6. **HTTP API reference** — table of routes; sub-section on SSE event
>    schema (`progress_start` / `progress_update` / `progress_end` /
>    `finish` / `cancelled` / `error` / `perf`).
> 7. **Output files & cleanup** — paths and lifecycle.
> 8. **Troubleshooting** — the common gotchas:
>    - `translate_engine_type` rejection,
>    - `openai_timeout` must be a string,
>    - `claude` CLI not found,
>    - "Task not finished" on download,
>    - "file must be a PDF",
>    - how to add a language code not in the dropdown.
>
> Keep tone practical, not marketing-fluffy. The document should be
> useful to someone who has never seen the project before.

---

## Prompt 9 — verification

> After everything is in place, run these and confirm:
>
> 1. `python -c "import ast; ast.parse(open('serve.py').read())"` →
>    OK.
> 2. `python -c "import serve; print(serve.app, serve.ALLOWED_MODELS,
>    serve.HARD_TARGET_LANG if hasattr(serve, 'HARD_TARGET_LANG') else
>    'no hard target')"` → prints the FastAPI app and confirms there
>    is no hard target language (target is whitelist-based, default
>    `zh`).
> 3. `python -c "import serve; cfg = serve.load_config(serve.CONFIG_PATH);
>    s = cfg.build_settings('en', 'zh'); tr = serve.make_translator(cfg,
>    s); print(tr.name, tr.lang_in, tr.lang_out)"` → prints
>    `openai en zh`.
> 4. `uvicorn serve:app --port 8765` then:
>    - `curl /health` → 200 with `active_model`.
>    - `curl /openapi.json` → 8 routes.
>    - `curl -X POST -F file=@config.json -F lang_in=zh -F lang_out=zh
>      /api/translate` → 422 "file must be a PDF".
>    - `curl -X POST -F file=@<any.pdf> -F lang_in=en -F lang_out=xx
>      /api/translate` → 422 "lang_out 'xx' not in [...]".
>    - `curl -X POST /api/reload-config` → 200.
> 5. In the browser at `http://localhost:8765/`, the source dropdown
>    should default to `en` and the target dropdown to `zh`. The
>    Cancel button should be hidden until Translate is clicked.
> 6. `tail -f` the server stdout during a real translation; you should
>    see lines like
>    `[taskid] translation start | engine=openai | ...`,
>    `[taskid]   stage START Translate Paragraphs (total=145)`,
>    `[taskid]   stage END   Translate Paragraphs (28.71s, items=145)`,
>    `[taskid] stage breakdown: ...`, plus the underlying BabelDOC
>    `il_translator` cache-hit messages at INFO level.

---

## Appendix A — common pitfalls the AI will hit

If the AI fails on one of these, paste the relevant **Prompt N** again
with the pitfall noted:

- **Pitfall 1 — `translate_engine_type` confusion.** The AI will often
  want to set this to the model name (`"MiniMax"`, `"DeepSeek"`, etc.).
  Re-paste Prompt 2's gotchas.
- **Pitfall 2 — `SettingsModel` reintroduction.** The AI will want to
  reuse `pdf2zh_next.config.model.SettingsModel` for type-safety. This
  brings back pydantic-Settings, four source-priority merging, and
  ~1000 lines. Re-paste Prompt 0 §5 and Prompt 3 §3.
- **Pitfall 3 — subprocess wrapper reintroduction.** The AI will see
  the upstream `_translate_in_subprocess` and assume SSE-friendly =
  subprocess-friendly. Re-paste Prompt 4 step 1 — `asyncio.create_task`
  in the FastAPI loop is fine.
- **Pitfall 4 — wrong `openai_timeout` type.** The Pydantic model
  expects `str`, not `int`. If the AI writes `"openai_timeout": 60` in
  `config.json`, the server fails to start with
  `Input should be a valid string`. Re-paste Prompt 2's second gotcha.
- **Pitfall 5 — letting `auto_extract_glossary` default to True.**
  Omitting the kwarg means BabelDOC's default kicks in and the
  translation takes 2× longer. Always pass
  `auto_extract_glossary=False` explicitly.
- **Pitfall 6 — `lang_out` from the UI defaulting to `auto`.** If the
  AI makes `auto` a valid target, the translation will fail in
  BabelDOC. Keep `auto` source-only.
- **Pitfall 7 — putting real secrets in `config.json`.** The placeholder
  `sk-REPLACE-ME` is fine for the template; instruct the user to
  replace it before running.
- **Pitfall 8 — adding CLI scripts to `pyproject.toml`.** If the AI
  re-adds `pdf2zh` / `pdf2zh2` / `pdf2zh_next` script entries, remove
  them — the only entry point is `python serve.py` or
  `uvicorn serve:app`.

---

## Appendix B — what the AI may reasonably push back on

These are points where the spec is opinionated. If the AI argues, here
is the rationale so you can decide:

- **"Why not use a frontend framework like htmx / Alpine.js?"** — the
  spec says vanilla JS for a 1-file service. Override only if you want
  a richer UI.
- **"Why not asyncpg / a real task queue?"** — the spec is OK with
  in-memory `TASKS` and filesystem-persisted outputs. The trade-off is
  that restarting the server loses the in-memory task table; the on-disk
  PDFs and the source PDF in `./uploads/` are still recoverable.
- **"Why not pydantic-Settings for the JSON config?"** — the spec
  reuses `OpenAISettings` / `ClaudeCodeSettings` (Pydantic `BaseModel`,
  not `BaseSettings`) for the engine block, and a plain
  `AppConfig` dataclass-like class for the rest. This avoids pulling in
  pydantic-settings and 4-source precedence merging.
- **"Why is `auto_extract_glossary` hard-coded off?"** — term
  extraction doubles latency on long PDFs and the downstream benefit
  is marginal for ad-hoc translation. Override by editing
  `serve.py:run_translation`.
