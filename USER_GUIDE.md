# PDF Translator — User Guide

A minimal local web service that translates PDF files between languages using
LLM APIs. Powered by [BabelDOC](https://github.com/funstory-ai/BabelDOC) for
PDF parsing/typography and a small FastAPI wrapper for the UI and HTTP API.

> Built on top of [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next).
> This is a stripped-down alternative to the upstream Gradio WebUI: only 2
> translation engines (OpenAI / ClaudeCode), only the languages surfaced in
> the dropdown, configuration via a single JSON file, and the UI is a single
> HTML page served by FastAPI.

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [How to use the app](#2-how-to-use-the-app)
3. [Configuration reference (config.json)](#3-configuration-reference-configjson)
   - 3.1 [Top-level](#31-top-level)
   - 3.2 [OpenAI engine](#32-openai-engine)
   - 3.3 [ClaudeCode engine](#33-claudecode-engine)
   - 3.4 [Translation behaviour](#34-translation-behaviour)
   - 3.5 [HTTP server](#35-http-server)
4. [Per-request options (UI)](#4-per-request-options-ui)
5. [Cancel a running task](#5-cancel-a-running-task)
6. [HTTP API reference](#6-http-api-reference)
7. [Output files & cleanup](#7-output-files--cleanup)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Quick start

### 1.1 Prerequisites

- Python 3.10–3.13
- An OpenAI-compatible API endpoint + key, **or** the `claude` CLI installed
  locally (for ClaudeCode engine)

### 1.2 Install

```bash
# Create and activate a virtual environment (conda example shown; venv works too)
conda create -n pdf2zh python=3.12 -y
conda activate pdf2zh

# Install this project in editable mode (pulls BabelDOC + deps)
pip install -e .

# (Optional) install web server deps if you do not already have them
pip install fastapi 'uvicorn[standard]' python-multipart
```

### 1.3 Edit `config.json`

Set at minimum:

- `active_model` — `"openai"` (default) or `"claudecode"`
- For OpenAI: fill `openai.openai_api_key`, `openai.openai_model`, and
  `openai.openai_base_url` (the last only if you are using a non-OpenAI
  OpenAI-compatible endpoint such as a self-hosted vLLM, DeepSeek, MiniMax,
  Zhipu, etc.)
- For ClaudeCode: ensure `claude` is on your `PATH` and
  `claudecode.claude_code_model` is set (e.g. `"sonnet"`)

### 1.4 Start the service

```bash
uvicorn serve:app --host 0.0.0.0 --port 8765
```

You should see:

```
INFO  serve: Loading config from .../config.json
INFO  Uvicorn running on http://0.0.0.0:8765
```

### 1.5 Open the UI

Visit <http://localhost:8765/> in a browser. You should see the translation
form with a file input, two language dropdowns (defaults: English → Chinese),
a **Translate** button, a progress bar, and a log area.

---

## 2. How to use the app

1. **Pick a source language** from the left dropdown (default: `en`).
2. **Pick a target language** from the right dropdown (default: `zh`).
3. **Click the file input** and choose a `.pdf` file from disk.
4. **Click Translate.** A red **Cancel** button appears next to it; the
   progress bar starts filling and the log area streams BabelDOC's internal
   stages.
5. **Watch progress.** The percentage and current stage (Parse PDF → Translate
   Paragraphs → Save PDF) update in real time.
6. **Click Cancel** any time to abort — see [§5](#5-cancel-a-running-task).
7. **When done**, four download links appear: dual (bilingual) and mono
   (translated-only) PDFs, each in a watermarked and a no-watermark variant.

The translated PDFs are also written to disk at:

```
./outputs/<task_id>/
    <basename>.mono.pdf
    <basename>.dual.pdf
    <basename>.mono.no_watermark.pdf
    <basename>.dual.no_watermark.pdf
```

so you can fetch them later by `task_id` via the API even if the browser tab
is closed.

---

## 3. Configuration reference (`config.json`)

The whole service is driven by one JSON file. Reload it without restarting
the server:

```bash
curl -X POST http://localhost:8765/api/reload-config
```

`config.json` has four sections.

### 3.1 Top-level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `active_model` | string | `"openai"` | Which translator to use. Must be `"openai"` or `"claudecode"`. |

### 3.2 OpenAI engine

This block is used when `active_model == "openai"`. The OpenAI Python client
is used; any OpenAI-compatible endpoint (real OpenAI, Azure-OpenAI-proxy,
vLLM, Ollama with the OpenAI shim, DeepSeek, MiniMax, Zhipu, etc.) can be
pointed at by changing `openai_base_url` and `openai_model`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `translate_engine_type` | string | `"OpenAI"` | Engine class identifier. **Must be `"OpenAI"`** to use the OpenAI-compatible translator. The actual model name lives in `openai_model` and is independent. |
| `openai_model` | string | `"gpt-4o-mini"` | Model ID sent in the chat-completion request (e.g. `gpt-4o`, `gpt-4o-mini`, `MiniMax-M3`, `deepseek-chat`). |
| `openai_base_url` | string \| null | `null` | API base URL. Leave `null` for real OpenAI, or set to your provider's URL. Trailing `/chat/completions` is stripped automatically. |
| `openai_api_key` | string \| null | `null` | API key. **Required.** |
| `openai_timeout` | string \| null | `null` | Per-request timeout in seconds. Must be a positive number. Use a string (`"60"`) in the JSON. |
| `openai_temperature` | string \| null | `null` | Sampling temperature, e.g. `"0"`, `"0.2"`. Only sent to the API if `openai_send_temprature` is `true`. |
| `openai_send_temprature` | bool | `false` | If `true`, the value in `openai_temperature` is included in the request payload. (Yes, the typo is preserved from the upstream project — see [issue #175](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/175).) |
| `openai_reasoning_effort` | string \| null | `null` | One of `minimal` / `low` / `medium` / `high`. Only sent if `openai_send_reasoning_effort` is `true`. Used by reasoning models. |
| `openai_send_reasoning_effort` | bool | `false` | If `true`, include `openai_reasoning_effort` in the request. |
| `openai_enable_json_mode` | bool | `false` | Enable JSON-object response mode. Generally leave `false` for translation; some providers require it for term extraction. |

### 3.3 ClaudeCode engine

This block is used when `active_model == "claudecode"`. The translation is
done by shelling out to the `claude` CLI.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `translate_engine_type` | string | `"ClaudeCode"` | Engine class identifier. **Must be `"ClaudeCode"`**. |
| `claude_code_path` | string | `"claude"` | Absolute path or `PATH` lookup for the `claude` binary. |
| `claude_code_model` | string | `"sonnet"` | Model name passed to the CLI (e.g. `sonnet`, `opus`, `haiku`). |

### 3.4 Translation behaviour

| Key | Type | Default | Meaning |
|---|---|---|---|
| `qps` | int | `4` | Queries-per-second cap for the LLM. Internal QPS limiter sleeps between requests. Set to `0` to disable. |
| `ignore_cache` | bool | `false` | If `true`, every text is re-translated even if it has been translated before. Translation results are stored at `~/.cache/pdf2zh_next/cache.v1.db` regardless. |
| `no_dual` | bool | `false` | Skip generating the **bilingual** PDF (original + translated side-by-side). |
| `no_mono` | bool | `false` | Skip generating the **monolingual translated-only** PDF. At least one of `no_dual` / `no_mono` must be `false`. |
| `watermark_output_mode` | string | `"watermarked"` | One of `watermarked` / `no_watermark` / `both` / `no watermark` (alias). Controls whether the output PDFs have a "translated by BabelDOC" footer. |
| `min_text_length` | int | `5` | Paragraphs shorter than this many characters are not translated. Avoids wasting LLM tokens on captions, page numbers, etc. |

> **Note:** automatic term extraction (`auto_extract_glossary`) is **off by
> default** in this service — it adds a full LLM pass over the document and
> is rarely worth the latency. To re-enable it, edit `serve.py` and change
> `auto_extract_glossary=False` to `True` in the `TranslationConfig(...)`
> call inside `run_translation()`.

### 3.5 HTTP server

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | string | `"0.0.0.0"` | Bind address. Use `127.0.0.1` to restrict to local. |
| `port` | int | `8765` | TCP port for the HTTP server. |

The path to the config file itself can be overridden with the environment
variable `PDF2ZH_CONFIG=/path/to/config.json`.

---

## 4. Per-request options (UI)

These are picked at translation time on the HTML form, not from
`config.json`.

| UI control | Possible values | Default | Meaning |
|---|---|---|---|
| Source language | `auto`, `en`, `zh`, `ja`, `fr`, `de`, `es`, `ru`, `ko`, `pt`, `it` | `en` | Language of the input PDF. `auto` lets BabelDOC auto-detect. |
| Target language | `en`, `zh`, `ja`, `fr`, `de`, `es`, `ru`, `ko`, `pt`, `it` | `zh` | Language to translate into. Other values are rejected with HTTP 422. |
| File picker | any `.pdf` | — | Single-file upload. Multi-file batch is not supported. |

The valid `lang_in` / `lang_out` codes follow ISO 639-1. BabelDOC itself
supports more languages (e.g. Arabic, Hindi, Vietnamese) but the dropdowns
in this UI are limited to the 10 most common to keep the UI compact. To
expose more, edit `ALLOWED_SOURCE_LANGS` / `ALLOWED_TARGET_LANGS` in
`serve.py`.

---

## 5. Cancel a running task

A long PDF translation can take 5–30 minutes. The UI provides a **Cancel**
button next to **Translate** while a task is running.

**What it does:**

1. The browser sends `POST /api/tasks/<task_id>/cancel`.
2. The server calls `.cancel()` on the asyncio task that runs the
   translation.
3. BabelDOC's internal `cancel_event` is set; the executor thread is asked
   to stop, the PDF writing is aborted.
4. The server emits a `{"type": "cancelled", "task_id": "..."}` event on the
   SSE stream.
5. The browser closes the EventSource, the Cancel button hides, and the
   Translate button re-enables.

The endpoint is **idempotent**: cancelling an already-finished task returns
200 with `{"already_finished": true}` and does nothing.

**Cleanup:** a cancelled task does not leave a partial PDF. The `outputs/<task_id>/`
directory may exist but is empty.

---

## 6. HTTP API reference

| Method & path | Purpose |
|---|---|
| `GET /` | Serves the HTML UI. |
| `GET /health` | `{"status":"ok","active_model":"openai"}` — quick liveness probe. |
| `POST /api/reload-config` | Re-read `config.json` from disk. Returns the new `active_model`. |
| `POST /api/translate` | Multipart upload. Fields: `file` (PDF), `lang_in`, `lang_out`. Returns `{"task_id": "<uuid>"}`. |
| `GET /api/tasks/{task_id}` | Returns `{"task_id","status","result","error"}` — `status` ∈ `pending`/`running`/`done`/`error`/`cancelled`. |
| `GET /api/tasks/{task_id}/events` | Server-Sent Events stream. Emits BabelDOC progress events verbatim. |
| `POST /api/tasks/{task_id}/cancel` | Cancel a running task. Idempotent. |
| `GET /api/tasks/{task_id}/download/{kind}` | Download a translated PDF. `kind` ∈ `dual` / `mono` / `dual_no_watermark` / `mono_no_watermark`. |
| `GET /openapi.json` | Full OpenAPI schema (handy for tooling). |

### Event stream schema (SSE)

Each event is a single line `data: {json}\n\n`. The JSON has at minimum a
`type` field:

| Event type | Extra fields | When emitted |
|---|---|---|
| `progress_start` | `stage`, `stage_progress`, `stage_current`, `stage_total` | A new stage begins. |
| `progress_update` | `stage`, `stage_progress`, `overall_progress`, `stage_current`, `stage_total` | Periodically while the current stage runs. |
| `progress_end` | `stage`, `stage_progress`, `stage_total` | Current stage finished. |
| `finish` | `result.{original_pdf_path, mono_pdf_path, dual_pdf_path, …, total_seconds}` | Translation succeeded. |
| `cancelled` | `task_id` | Sent when the user clicked Cancel. |
| `error` | `error` | Translation failed. The task status becomes `error`. |

The server also sends a `: keepalive\n\n` comment every 15 seconds to keep
proxies from closing the connection.

---

## 7. Output files & cleanup

| Path | Lifecycle | Notes |
|---|---|---|
| `./uploads/<task_id>.pdf` | Kept forever | The original PDF you uploaded. Safe to delete after translation succeeds. |
| `./outputs/<task_id>/*.pdf` | Kept forever | The translated PDFs. Up to 4 files depending on `watermark_output_mode` / `no_dual` / `no_mono`. |
| `~/.cache/pdf2zh_next/cache.v1.db` | Grows over time; auto-pruned at 50,000 rows by BabelDOC. | Per-`text`-hash translation cache. Set `ignore_cache: true` in `config.json` to bypass reads. |
| `./uploads/*.pdf` (orphan) | Manual cleanup | If the service crashes between upload and task creation, the file is orphaned. |

There is no automatic cleanup of `./outputs/` — this is intentional so you
can re-download a translation by `task_id` after closing the browser.

To free space:

```bash
rm -rf outputs/   # removes all translated PDFs
rm -rf uploads/   # removes all original uploads
```

---

## 8. Troubleshooting

### "Validation error: translate_engine_type Input should be 'OpenAI'"

You set `translate_engine_type` to your model name (e.g. `"MiniMax"` or
`"DeepSeek"`). It must stay `"OpenAI"` — this is the engine class
identifier, not the model. Put your model in `openai_model` and your
provider in `openai_base_url`.

### "No module named 'babeldoc'"

Install the dependency:

```bash
pip install 'babeldoc>=0.6.2,<0.7.0'
```

Or reinstall the project: `pip install -e .`

### "openai_timeout Input should be a valid string"

`openai_timeout` is read as a string in the JSON (Pydantic strict type). Use
`"60"` not `60`.

### Translation is very slow

- The first run on a model pulls the model + warms BabelDOC's
  fonts/layout-model assets. Subsequent translations of any PDF are
  much faster.
- Long PDFs are auto-split into parts internally; each part is
  translated in parallel up to `qps` concurrent requests.
- Disable `auto_extract_glossary` (already off by default in this service)
  to skip a full extra LLM pass.
- If you are rate-limited (HTTP 429), lower `qps` in `config.json`.

### "Claude Code CLI not found at 'claude'"

Either install the `claude` CLI and put it on `PATH`, or set
`claudecode.claude_code_path` to the absolute path.

### "Task not finished" when downloading

The translation is still running, or it failed. Check `GET
/api/tasks/<id>` to see `status`. If `error`, see the server logs.

### "file must be a PDF"

The file you uploaded is not a `.pdf` (extension check is case-insensitive).
The validation runs before the file is read, so the upload is rejected
without saving.

### I want to use a language not in the dropdown

Edit `ALLOWED_SOURCE_LANGS` and `ALLOWED_TARGET_LANGS` in `serve.py` to
include your language code, then rebuild the HTML by re-saving `serve.py`
(it is a single triple-quoted string).

### Port already in use

Either change `server.port` in `config.json` and reload, or stop the
existing process:

```bash
lsof -i :8765         # find PID
kill <pid>
```

---

## See also

- [PLAN.md](PLAN.md) — design and implementation plan
- [README.md](README.md) — upstream project documentation
- BabelDOC upstream: <https://github.com/funstory-ai/BabelDOC>
