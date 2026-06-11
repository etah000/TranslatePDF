# FastAPI + HTML 翻译服务 — 实施计划

## Context（背景）

`pdf2zh_next` 当前 10,410 行 Python 里的核心翻译链路（PDF 解析 / 段落拆分 / 限流 / 缓存 / 写回 PDF）全部由依赖 `babeldoc` 提供；本仓库的代码 95% 是**产品化包装**（Gradio WebUI、24 个翻译服务适配器、4 套配置源合并、8 语言文档、子进程隔离、Windows EXE 打包等）。

本计划要做的事情：

> 用 **FastAPI + 极简 HTML** 替代 Gradio WebUI，**模型只保留 OpenAI 与 ClaudeCode 两种**，**只翻译到英文**，配置改用 **JSON 配置文件**驱动，**翻译界面只保留 4 个元素**：输入文件 / 翻译语言 / 输出 / 进度与状态日志。

目标是把"门面"做小，**翻译逻辑仍全部走 `babeldoc.format.pdf.high_level.async_translate`**，并尽可能复用 `pdf2zh_next` 中已经写好的两个 translator 实现（`OpenAITranslator`、`ClaudeCodeTranslator`），避免重写 LLM 客户端代码。

---

## 功能需求（Functional Requirements）

### FR-1 配置文件

- 一个 JSON 文件（例如 `config.json`），结构如下（**初版**）：
  ```json
  {
    "active_model": "openai",
    "openai": {
      "model": "gpt-4o-mini",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-...",
      "timeout": 60,
      "qps": 4,
      "temperature": 0.0,
      "send_temperature": false,
      "send_reasoning_effort": false,
      "enable_json_mode": false
    },
    "claudecode": {
      "path": "claude",
      "model": "sonnet",
      "qps": 4
    },
    "translation": {
      "lang_in": "en",
      "lang_out": "zh",
      "ignore_cache": false,
      "no_dual": false,
      "no_mono": false,
      "watermark_output_mode": "no watermark"
    },
    "server": { "host": "0.0.0.0", "port": 8765 }
  }
  ```
- 配置文件路径可通过环境变量 `PDF2ZH_CONFIG` 覆盖，**启动时一次性读入**，运行中变更需要重启（或提供手动 reload 接口，见 NFR-3）。

### FR-2 翻译模型

- **仅支持两个模型**：`openai`、`claudecode`。
- 切换通过配置文件 `active_model` 控制（不再在 UI 里切，避免动态分支爆炸）。
- 不支持运行中热切换；切换 = 改 JSON + 调 reload 接口。

### FR-3 语言

- **源语言**：用户可在 UI 上选择（`auto` / `zh` / `ja` / `fr` / `de` 等，BabelDoc 支持的语种白名单），默认 `zh`。
- **目标语言**：硬约束只允许 `en`（英语），其他取值在请求校验阶段直接 422。
- 前端 UI：**两个下拉框**——`Source language`（多选项）和 `Target language`（单选项 `English`，后端二次校验）。

### FR-4 翻译界面（HTML 页面，单页）

4 个区域：

| 区域 | 元素 | 说明 |
|---|---|---|
| 1. 输入 | `<input type="file" accept="application/pdf">` | 单文件上传 |
| 2. 翻译语言 | `<input>` 显示 `English` (read-only) | 仅展示 |
| 3. 输出 | 一个 `<div>` + 三个下载按钮 | 显示生成的 PDF 路径与"下载 dual / mono / no-watermarked"链接 |
| 4. 进度 + 状态日志 | 进度条 + 滚动日志框 | 后端 SSE 推送 |

页面风格：原生 HTML + 一小段 vanilla JS（fetch + EventSource），**不引第三方前端框架**。

**根据用户决定：不提供"翻译前原 PDF 预览"按钮**——翻译完成后直接展示下载链接。

### FR-5 翻译接口

- `POST /api/translate`  
  multipart/form-data 上传 PDF 文件 → 返回 `task_id`。
- `GET /api/tasks/{task_id}/events`  
  Server-Sent Events 流，向前端推送 `progress_start` / `progress_update` / `progress_end` / `finish` / `cancelled` / `error` 事件（与 `babeldoc.async_translate` 原生事件 schema 完全一致，见 NFR-1）。
- `GET /api/tasks/{task_id}`  
  查询任务状态 + 输出文件路径。
- `GET /api/tasks/{task_id}/download/{kind}`  
  下载翻译后的 PDF（kind ∈ `dual` / `mono` / `dual_no_watermark` / `mono_no_watermark`）。
- `POST /api/tasks/{task_id}/cancel`  
  取消正在执行的翻译任务。幂等 —— 已完成的任务返回 `already_finished=True`。
- `POST /api/reload-config`  
  重新读取 JSON 配置文件（用于切换模型）。

### FR-7 取消按钮

- 翻译启动后,UI 上"Translate" 按钮变为 disabled,旁边出现一个红色的 **Cancel** 按钮。
- 点击 Cancel → 前端调 `POST /api/tasks/{id}/cancel` → 服务端对 worker 的 `asyncio.Task` 调用 `.cancel()` → BabelDoc 内部的 `cancel_event` 被 set,`async_translate` 退出 → 任务状态变 `cancelled`,SSE 流推一条 `{"type": "cancelled"}` 事件 → 前端关闭 EventSource、隐藏 Cancel 按钮、恢复 Translate 按钮。
- 翻译过程中,**`auto_extract_glossary=False`**(见 FR-6 的"term 提取缺省关闭"决定),省掉一轮 LLM 术语抽取。

### FR-8 Term 提取缺省关闭

- 翻译一次 PDF, BabelDoc 默认会自动调一遍 LLM 做术语抽取(`AutomaticTermExtractor`),对长 PDF 很耗时。
- 用户决定: **默认关闭**。
- 实现: 在 `run_translation` 构造 `TranslationConfig` 时,固定传 `auto_extract_glossary=False`。
- 副作用: `BabelDOCConfig.term_extraction_translator` 仍传一个 translator 实例,但 BabelDoc 看到 `auto_extract_glossary=False` 就不会真正调用它。

### FR-6 翻译逻辑

- 复用 `pdf2zh_next.translator.translator_impl.openai.OpenAITranslator` 和 `ClaudeCodeTranslator`。
- 复用 `pdf2zh_next.translator.utils.get_rate_limiter` 创建 `QPSRateLimiter`。
- 复用 `pdf2zh_next.translator.cache.init_db` 初始化 SQLite 缓存（位于 `~/.cache/pdf2zh_next/cache.v1.db`，与现有行为一致）。
- 翻译在 **FastAPI 进程内**用 `asyncio.create_task` 调度，**不另起子进程**——理由：FastAPI 是 asyncio 主循环，BabelDoc 的 `async_translate` 本身就是为协程设计的；用子进程反而会失去 SSE 实时性。但要保留一个 `try/except` 捕获 BabelDoc 抛出的异常并转化为 SSE `error` 事件。

---

## 非功能需求（NFR）

- **NFR-1 事件契约**：与现有 `pdf2zh_next.high_level.do_translate_async_stream` 完全一致——`type` ∈ {`stage_summary`, `progress_start`, `progress_update`, `progress_end`, `finish`, `error`}。前端解析逻辑可直接复用 `docs/en/advanced/API/python.md` 的描述。
- **NFR-2 配置即真相**：除用户上传的 PDF 文件之外，所有参数都从 JSON 读；不暴露其他 UI 配置项。
- **NFR-3 错误处理**：文件不是 PDF → 422；目标语言不是 `en` → 422；模型名不在白名单 → 422；翻译过程中 BabelDoc 抛错 → 通过 SSE `error` 事件推送。
- **NFR-4 单一文件**：整个 FastAPI 服务用 **1 个 Python 文件**（约 250~350 行）实现，HTML 内嵌在 Python 三引号字符串里，避免任何静态资源路由。

---

## 设计要点

### 1. 复用 vs 重写

| 复用（不改） | 来源 |
|---|---|
| `OpenAITranslator` / `ClaudeCodeTranslator` | `pdf2zh_next/translator/translator_impl/{openai,claudecode}.py` |
| `QPSRateLimiter` + `BaseRateLimiter` | `pdf2zh_next/translator/rate_limiter/qps_rate_limiter.py` |
| `TranslationCache` + `init_db` | `pdf2zh_next/translator/cache.py` |
| `babeldoc.format.pdf.high_level.async_translate` | BabelDoc（依赖） |
| `babeldoc.format.pdf.translation_config.TranslationConfig` | BabelDoc（依赖） |
| `babeldoc.format.pdf.translation_config.WatermarkOutputMode` | BabelDoc（依赖） |

| 新写 | 原因 |
|---|---|
| `app.py` (~300 行) | FastAPI 入口 + 4 个路由 + 内嵌 HTML |
| `config.json` | 用户配置 |
| `requirements.txt` 追加 | `fastapi`, `uvicorn[standard]`, `python-multipart`（其余继承自 `pyproject.toml`） |

**翻译配置（`TranslationConfig`）直接用 BabelDoc 原生 dataclass，** 不再走 pdf2zh_next 的 Pydantic SettingsModel——这是减少代码量的关键。

### 2. Translator 的"模型拼装"问题

现有 `OpenAITranslator.__init__` 接受 `settings.translate_engine_settings.openai_*` 字段。我们需要：

- 写一个轻量级 `_OpenAIEngineSettings`（plain `@dataclass`，不引 pydantic），从 JSON 读入；
- 在 `OpenAITranslator.__init__` 之前 monkey-patch 一个最薄的 shim，让它能从一个 dataclass 读到 `openai_api_key` 等字段。

**实现选择**：
- **方案 A（推荐）**：直接 `from pdf2zh_next.config.translate_engine_model import OpenAISettings; OpenAISettings(**json_dict)` 复用现有 Pydantic 模型；CluadeCodeSettings 同理。零代码量，校验逻辑免费用。
- 方案 B：写个临时 `SimpleNamespace` 或 dataclass 凑齐字段。

**选 A**。

### 3. SettingsModel 的去向

- **不再构造** `pdf2zh_next.config.model.SettingsModel`。
- `OpenAISettings` 和 `ClaudeCodeSettings` 在 `translate_engine_model.py` 里都实现了 `validate_settings()`，校验仍能工作。
- 唯一损失：`SettingsModel.validate_settings()` 里那段"同时自动设置 term_extraction_translator"等联动逻辑——**直接复用 `OpenAISettings` 作为 `term_extraction_translator`**，因为 OpenAI/ClaudeCode 都 `support_llm="yes"`，能跑自动术语提取。

### 4. Translator 注入到 BabelDoc 的方式

```python
from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode

translator = OpenAITranslator(temp_settings, QPSRateLimiter(qps))
config = TranslationConfig(
    input_file=pdf_path,
    translator=translator,
    lang_in="auto",
    lang_out="en",
    doc_layout_model=None,
    output_dir=out_dir,
    qps=qps,
    pool_max_workers=pool_max_workers,
    watermark_output_mode=WatermarkOutputMode(watermark_str),
    no_dual=no_dual,
    no_mono=no_mono,
    report_interval=0.1,
)
async for event in async_translate(config):
    await sse_queue.put(event)
```

### 5. 任务管理

- 全局 `dict[str, asyncio.Queue]` 存 `task_id → 事件队列`。
- 一个翻译对应一个 `task_id`（uuid4），文件存到 `./uploads/{task_id}.pdf`，输出到 `./outputs/{task_id}/`。
- 简单的内存任务表 + 文件落地。不引入 Celery / Redis / 数据库。

### 6. HTML 页面

内嵌在 `app.py` 的三引号字符串里，结构如下：

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>PDF Translator</title>
<style>/* 200 行内联 CSS：布局、按钮、进度条、日志框 */</style></head>
<body>
  <h1>PDF Translator</h1>
  <div>
    <label>Source language:
      <select id="langIn">
        <option value="auto" selected>auto</option>
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
    <label>Target language:
      <select id="langOut">
        <option value="en" selected>English (en)</option>
      </select>
    </label>
  </div>
  <input type="file" id="file" accept="application/pdf">
  <button id="go">Translate</button>
  <progress id="bar" value="0" max="100"></progress>
  <span id="pct">0%</span>
  <pre id="log" style="height:200px; overflow:auto; background:#111; color:#0f0"></pre>
  <div id="downloads"></div>
<script>/* 60 行 vanilla JS：fetch + EventSource */</script>
</body>
</html>
```

---

## 文件结构（最终新增/修改）

```
PDFMathTranslate-next/
├── PLAN.md                              ← 本文件
├── config.json                          ← 新增：用户配置
├── serve.py                             ← 新增：FastAPI 应用（~300 行）
├── pyproject.toml                       ← 追加 fastapi/uvicorn/python-multipart；删除 [project.scripts]
├── uploads/                             ← 运行时生成：上传的源 PDF
└── outputs/                             ← 运行时生成：翻译产物（永久保留）
```

`pdf2zh_next/` 子目录**完全不动**——所有现有 translator、cache、rate_limiter 都按原样复用。`pdf2zh_next/main.py` 和 `cli()` 仍保留但不再暴露为可执行脚本。

---

## 实施步骤

| 步骤 | 任务 | 涉及文件 |
|---|---|---|
| 1 | 写 `config.json` 默认模板 | `config.json` |
| 2 | 实现 `serve.py`：配置加载 + Translator 工厂 | `serve.py` |
| 3 | 实现 `POST /api/translate`：接收文件、写盘、起 `asyncio.Task` 跑 `async_translate`、把事件入队 | `serve.py` |
| 4 | 实现 `GET /api/tasks/{id}/events`：从队列里 `get` 事件并以 SSE 格式 `data: {json}\n\n` 推送给浏览器 | `serve.py` |
| 5 | 实现 `GET /api/tasks/{id}`：查状态 | `serve.py` |
| 6 | 实现 `GET /api/tasks/{id}/download/{kind}`：用 `FileResponse` 推 PDF | `serve.py` |
| 7 | 实现 `POST /api/reload-config` | `serve.py` |
| 8 | 写内嵌 HTML 页面 + JS：上传、SSE 订阅、显示进度、追加日志、生成下载链接 | `serve.py`（HTML 在三引号里） |
| 9 | 更新 `pyproject.toml`：添加 `fastapi`, `uvicorn[standard]`, `python-multipart`；删除 `[project.scripts]` 中三个 pdf2zh 入口 | `pyproject.toml` |
| 10 | 端到端验证（见下） | — |

---

## 关键文件 / 复用点速查

| 需要复用的对象 | 完整路径 | 备注 |
|---|---|---|
| OpenAI Translator | `pdf2zh_next/translator/translator_impl/openai.py:OpenAITranslator` | 读 `settings.translate_engine_settings.openai_*` |
| ClaudeCode Translator | `pdf2zh_next/translator/translator_impl/claudecode.py:ClaudeCodeTranslator` | 读 `settings.translate_engine_settings.claude_code_*` |
| QPS 限流 | `pdf2zh_next/translator/rate_limiter/qps_rate_limiter.py:QPSRateLimiter` | |
| 缓存 | `pdf2zh_next/translator/cache.py:init_db / TranslationCache` | 文件 `~/.cache/pdf2zh_next/cache.v1.db` |
| Pydantic 设置 | `pdf2zh_next/config/translate_engine_model.py:OpenAISettings / ClaudeCodeSettings` | 用 `**dict` 构造 |
| BabelDoc 入口 | `babeldoc.format.pdf.high_level.async_translate` | async generator，yield 进度事件 |
| BabelDoc 配置 | `babeldoc.format.pdf.translation_config.TranslationConfig / WatermarkOutputMode` | 直接构造 |
| 事件 schema | `docs/en/advanced/API/python.md` §"Event Stream Contract" | 复制到 HTML 的注释里 |

---

## 验证（Verification）

启动：

```bash
pip install fastapi 'uvicorn[standard]' python-multipart
# config.json 里填入 OPENAI_API_KEY
uvicorn serve:app --host 0.0.0.0 --port 8765
```

手工端到端测试（用 curl + 浏览器）：

1. **健康检查**
   ```bash
   curl -s http://localhost:8765/health
   # 期望：{"status":"ok","active_model":"openai"}
   ```

2. **上传 PDF + 接收 task_id**
   ```bash
   curl -s -F file=@samples/short.pdf http://localhost:8765/api/translate
   # 期望：{"task_id":"<uuid>"}
   ```

3. **浏览器订阅 SSE**
   浏览器打开 `http://localhost:8765/`，上传文件 → 应看到进度条从 0% 走到 100%，日志框滚动出现 `progress_start` / `progress_update` / `finish` 事件。

4. **下载结果**
   任务完成后页面应出现 3 个下载按钮：`dual.pdf` / `mono.pdf` / `dual-no-watermark.pdf`。
   ```bash
   curl -sOJ http://localhost:8765/api/tasks/<id>/download/dual
   ```

5. **目标语言校验**
   ```bash
   # 假定请求 body 中带 lang_out 字段
   curl -s -F file=@a.pdf -F lang_out=fr http://localhost:8765/api/translate
   # 期望：422 {"detail":"Only target language 'en' is supported"}
   ```

6. **模型白名单校验**
   改 `config.json` 的 `active_model` 为 `"grok"` → `curl /api/reload-config` → 再上传 → 期望 422 `{"detail":"active_model 'grok' is not supported"}`。

7. **缓存命中验证**
   同一篇 PDF 翻译两次 → 第二次日志里应看到 cache hit 计数，翻译时间明显缩短（验证 `TranslationCache` 路径有效）。

8. **崩溃隔离（可选）**
   故意传一个被截断的 `bad.pdf` → SSE 应推 `error` 事件，FastAPI 进程不应崩。

---

## 风险与权衡

| 风险 | 缓解 |
|---|---|
| 把翻译跑在主进程里，坏 PDF 拖死 FastAPI | 已有 `try/except` + SSE `error` 事件；如果用户担心可后续把 `_run_in_subprocess` 重新启用 |
| 删掉 Gradio 后失去 PDF 浏览器内嵌预览 | BabelDoc 的产物是 PDF 文件，HTML 用 `<a download>` 即可；**已确认：不提供"翻译前预览"** |
| 多文件 / 多任务并发导致 SQLite 锁 | BabelDoc 自带 `journal_mode=wal`，并发写可接受；并发任务数建议 < 4 |
| 配置文件热重载线程安全 | 用 `asyncio.Lock` 保护 `active_model` 切换 |
| `./outputs/{task_id}/` 永久不删，磁盘增长 | **已确认：用户接受**；可后续加个 `make clean-outputs` 手动清理脚本 |

---

## FAQ（执行时需要确认的点）

- **Q：是否需要"翻译前预览原 PDF"？**  
  A：**已确认不需要**。翻译完成后直接展示下载链接。

- **Q：源语言怎么选？**  
  A：**已确认**：UI 提供 `auto / zh / ja / fr / de / es / ru / ko / pt / it` 等 BabelDoc 支持的常用语种下拉，默认 `en`。

- **Q：输出文件如何清理？**  
  A：**已确认**：保留在 `./outputs/{task_id}/`，永久不删。磁盘增长由用户自行处理。

- **Q：是否保留 `pdf2zh` CLI 入口？**  
  A：**已确认：不保留**。  
  - 从 `pyproject.toml` 中删除 `pdf2zh` / `pdf2zh2` / `pdf2zh_next` 三个 `[project.scripts]` 条目；  
  - **`pdf2zh_next/main.py` 与 `cli()` 函数可保留作为内部 utility**（也可彻底删除，本计划选保留以便 `__init__` 中的 import 不破）；  
  - **唯一对外入口** 是 `uvicorn serve:app`。

- **Q：是否要 Docker 化？**  
  A：**不需要**； 

- **Q：是否需要支持 OpenAI 兼容接口（如 DeepSeek、自部署 vLLM）？**  
  A：FR-2 限定为 `openai` 模型；JSON 里 `openai.base_url` 字段已允许指向任意 OpenAI 兼容端点，所以"换 base_url = 换模型"已经隐式支持，**无需新增配置项**。
