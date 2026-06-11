# 详细设计文档 — PDF 翻译服务（FastAPI）

> 面向维护者的软件工程式详细设计文档。目标读者：此前未见过此项目的程序员，需要修 bug 或扩展功能时可依据本文档快速定位。文档中的行号与当前提交的 `serve.py` — 一对应；若正文与代码发生偏差，以代码为准。

---

## 文档组织说明

全文按四部分组织，遵循自然的阅读顺序：先从鸟瞰视图入手，再深入各组件实现，接着审视横切关注点，最后以运维与扩展内容收尾。

- **第一篇 — 概述与架构**（§1–§5）：服务是什么、特意不做什么、源码树如何布局。
- **第二篇 — 核心实现**（§6–§11）：真正的代码 —— 配置、翻译器、工作协程、内存状态、HTTP 路由与内嵌前端。
- **第三篇 — 横切关注点**（§12–§15）：日志、取消、错误处理、外部依赖。这些话题各自触及第二篇的多个组件。
- **第四篇 — 运维与扩展**（§16–§19）：如何扩展服务、常见陷阱、部署运行、术语表。

---

## 目录

### 第一篇 — 概述与架构

1. [目的与范围](#1-目的与范围)
2. [设计目标与非目标](#2-设计目标与非目标)
3. [高层架构](#3-高层架构)
4. [文件布局](#4-文件布局)
5. [`serve.py` 模块划分](#5-servepy-模块划分)

### 第二篇 — 核心实现

6. [配置（`config.json` + `AppConfig`）](#6-配置configjson--appconfig)
   - 6.1 [配置文件格式](#61-配置文件格式)
   - 6.2 [`openai` 引擎配置块](#62-openai-引擎配置块)
   - 6.3 [`claudecode` 引擎配置块](#63-claudecode-引擎配置块)
   - 6.4 [`translation` 翻译配置块](#64-translation-翻译配置块)
   - 6.5 [`server` 服务配置块](#65-server-服务配置块)
   - 6.6 [配置文件路径](#66-配置文件路径)
   - 6.7 [`AppConfig` 类](#67-appconfig-类)
   - 6.8 [热重载](#68-热重载)
7. [翻译器类（`OpenAITranslator`、`ClaudeCodeTranslator`）](#7-翻译器类openaitranslatorclaudecodetranslator)
   - 7.1 [OpenAI 翻译器](#71-openai-翻译器)
   - 7.2 [ClaudeCode 翻译器](#72-claudecode-翻译器)
   - 7.3 [公共模式](#73-公共模式)
   - 7.4 [翻译提示词辅助函数](#74-翻译提示词辅助函数)
8. [翻译工作协程（`run_translation`）](#8-翻译工作协程run_translation)
   - 8.1 [关键不变量](#81-关键不变量)
   - 8.2 [任务生命周期](#82-任务生命周期)
9. [内存任务表与 SSE 队列](#9-内存任务表与-sse-队列)
   - 9.1 [Task 类](#91-task-类)
   - 9.2 [TASKS 字典与 TASK_LOCK](#92-tasks-字典与-task_lock)
   - 9.3 [CONFIG 与 CONFIG_LOCK](#93-config-与-config_lock)
   - 9.4 [生命周期](#94-生命周期)
10. [HTTP 路由](#10-http-路由)
    - 10.1 [健康检查](#101-健康检查)
    - 10.2 [重载配置](#102-重载配置)
    - 10.3 [提交翻译](#103-提交翻译)
    - 10.4 [查询任务状态](#104-查询任务状态)
    - 10.5 [取消任务](#105-取消任务)
    - 10.6 [SSE 事件流](#106-sse-事件流)
    - 10.7 [下载译文](#107-下载译文)
    - 10.8 [首页](#108-首页)
11. [内嵌 HTML/JS 前端](#11-内嵌-htmljs-前端)
    - 11.1 [页面布局](#111-页面布局)
    - 11.2 [JS 状态机](#112-js-状态机)
    - 11.3 [关键函数](#113-关键函数)

### 第三篇 — 横切关注点

12. [日志](#12-日志)
    - 12.1 [两条日志流](#121-两条日志流)
    - 12.2 [服务端逐任务日志](#122-服务端逐任务日志)
    - 12.3 [被提升到 INFO 的 BabelDoc logger](#123-被提升到-info-的-babeldoc-logger)
    - 12.4 [日志格式](#124-日志格式)
13. [取消语义](#13-取消语义)
    - 13.1 [两层取消](#131-两层取消)
    - 13.2 [取消后保留的内容](#132-取消后保留的内容)
14. [错误处理](#14-错误处理)
    - 14.1 [校验错误（4xx）](#141-校验错误4xx)
    - 14.2 [翻译错误（等效 5xx）](#142-翻译错误等效-5xx)
    - 14.3 [启动错误](#143-启动错误)
    - 14.4 [ClaudeCode 子进程错误](#144-claudecode-子进程错误)
15. [外部依赖](#15-外部依赖)
    - 15.1 [PyPI 包](#151-pypi-包)
    - 15.2 [外部 CLI / 服务](#152-外部-cli--服务)
    - 15.3 [文件系统](#153-文件系统)

### 第四篇 — 运维与扩展

16. [扩展指南](#16-扩展指南)
    - 16.1 [新增 LLM 引擎](#161-新增-llm-引擎)
    - 16.2 [新增 UI 语言](#162-新增-ui-语言)
    - 16.3 [开启术语抽取](#163-开启术语抽取)
    - 16.4 [持久化 TASKS 到磁盘](#164-持久化-tasks-到磁盘)
    - 16.5 [切换 gunicorn / 多 worker](#165-切换-gunicorn--多-worker)
17. [已知问题排查](#17-已知问题排查)
18. [构建、运行、部署](#18-构建运行部署)
    - 18.1 [安装](#181-安装)
    - 18.2 [配置](#182-配置)
    - 18.3 [运行](#183-运行)
    - 18.4 [Docker](#184-docker)
    - 18.5 [日志查看](#185-日志查看)
19. [术语表](#19-术语表)

---

# 第一篇 — 概述与架构

> 鸟瞰视图。先读此篇，了解服务做什么、特意不做什么、源码树如何组织。

---

## 1. 目的与范围

一个单文件 FastAPI HTTP 服务，使用大语言模型将 PDF 文件在人类语言之间互译。每次上传一个 PDF，展示实时进度，翻译完成后提供译文下载。

**关键引用：**

- **BabelDoc**（PyPI 上的 `babeldoc` 包）承担实际的 PDF 解析、段落拆分、版面分析、术语抽取（开启时）以及双语/单语 PDF 输出。
- **本服务**在此基础上增加了：HTTP API、HTML/JS 界面、两个具体的 LLM 适配器、JSON 文件驱动的配置、取消按钮、逐阶段性能日志，以及一个小型进程内任务表。

---

## 2. 设计目标与非目标

### 设计目标

1. **仓库根目录下仅一个 Python 文件。** 无 Python package、无 `src/` 布局、无 `__init__.py` 迷宫。导入流经 `babeldoc.*` 及少量标准 PyPI 包。
2. **单个 JSON 文件驱动配置**（`config.json`），运行时可通过 `POST /api/reload-config` 重新读取。
3. **仅两个 LLM 引擎**（`openai`、`claudecode`）；上游 `pdf2zh_next` 包中的其余引擎全部移除。
4. **HTML 界面为单页面**，以内联 Python 字符串形式提供，无静态资源、无 JS 框架、无构建步骤。
5. **通过 SSE 提供实时进度** —— 每个任务一条事件流，事件源为 BabelDoc 自身产生的事件。
6. **可取消的翻译任务** —— 通过专用 HTTP 端点取消后台 `asyncio.Task`。
7. **可观察的性能数据** —— BabelDoc 每个阶段（stage）的起止时刻在服务端打时间戳，并作为 `perf` SSE 事件发送给前端。
8. **不使用 Pydantic Settings** —— 引擎参数用纯 `dict` / `SimpleNamespace` 承载；Pydantic 仅作为 BabelDoc 的传递依赖存在，本代码库不直接使用。

### 非目标

1. **多租户** —— 无认证、无每用户限速、无配额。如需锁定，绑定 `127.0.0.1`。
2. **多文件批处理** —— `/api/translate` 每次仅接受一个 PDF。
3. **持久化任务历史** —— `TASKS` 字典在进程内；重启后丢失（但磁盘上的 `./outputs/{task_id}/*.pdf` 和 `./uploads/{task_id}.pdf` 仍然存在）。
4. **子进程隔离** —— 与上游 `pdf2zh_next` 不同，翻译在 FastAPI 服务所在进程内运行。
5. **更丰富的前端** —— 不使用 Alpine/htmx/Vue，无拖拽，无原始 PDF 预览。

---

## 3. 高层架构

```
                ┌──────────────────────────────────────────────────────────────┐
                │  浏览器（原生 JS）                                            │
                │  ─ 文件选择器、语言下拉框、Translate/Cancel 按钮、日志区域      │
                └───────────────────────┬──────────────────────────────────────┘
                                        │ multipart 上传
                                        ▼
              ┌────────────────────── /api/translate ──────────────────────────┐
              │  • 保存上传文件到 ./uploads/{task_id}.pdf                       │
              │  • 在 TASKS 字典中创建 Task 对象                                │
              │  • 启动 asyncio.create_task(run_translation(...))              │
              │  • 返回 { task_id }                                           │
              └───────────────────────┬──────────────────────────────────────┘
                                      │
                                      ▼
              ┌────────────────────── run_translation() ────────────────────────┐
              │  • 构造 OpenAITranslator / ClaudeCodeTranslator                 │
              │  • 调用 set_translate_rate_limiter(cfg.qps)                    │
              │  • async for event in babeldoc.async_translate(config):        │
              │      - 逐阶段计时（stage_start / stage_end 日志行）             │
              │      - 逐事件入队（task.events.put(event)）                     │
              │      - 收到 finish 事件时：提取路径，构造 result 字典           │
              │  • 捕获 CancelledError → 发送 "cancelled" 事件                  │
              │  • 捕获 Exception      → 发送 "error" 事件                     │
              └───────────────────────┬──────────────────────────────────────┘
                                      │ 事件流
                                      ▼
        ┌────────────────── /api/tasks/{id}/events (SSE) ─────────────────────┐
        │  从 task.events 读取；格式化为 "data: {json}\n\n"；15 秒心跳保活      │
        │  None 哨兵关闭流                                                     │
        └───────────────────────┬──────────────────────────────────────────────┘
                                │
                                ▼
        ┌──────────────────── /api/tasks/{id}/download/{kind} ─────────────────┐
        │  4 种类型：dual, mono, dual_no_watermark, mono_no_watermark         │
        │  返回 FileResponse(application/pdf)                                 │
        └──────────────────────────────────────────────────────────────────────┘
```

整个服务运行在**单个 asyncio 事件循环**上：FastAPI 处理器、SSE 生成器、翻译工作协程全部共享同一循环。翻译过程是 CPU+IO 混合负载（解析 + LLM HTTP 调用）；并发由 BabelDoc 内部的 `ThreadPoolExecutor` 提供（线程池大小 = `qps`）。

---

## 4. 文件布局

```
PDFMathTranslate-next/
├── serve.py                915 行   ← 主应用程序
├── config.json             32 行    ← 运行时配置
├── pyproject.toml          161 行   ← 依赖声明
├── setup.cfg                        ← 遗留文件
├── Dockerfile, .dockerignore        ← 继承自上游
├── LICENSE, .gitignore              ← 继承自上游
├── PLAN.md                           ← 高层计划
├── USER_GUIDE.md                     ← 用户手册
├── requirement-prompts.md            ← AI 代码生成提示词集
├── detailed-design.md                ← 本文档
├── README.md                         ← 上游原始 README（已过时）
├── outputs/                          ← 运行时：翻译产物（永久保留）
└── uploads/                          ← 运行时：原始 PDF（永久保留）
```

**不存在** `pdf2zh_next/` 包、`__init__.py`、`tests/`、`docs/`、`script/`。以上内容均在重构为单文件服务时被移除。

---

## 5. `serve.py` 模块划分

`serve.py` 是一个 915 行的文件，按以下顺序组织各节：

| 行号（约） | 节 | 作用 |
|---|---|---|
| 1–24  | 标准库 + 第三方 import | `asyncio`, `json`, `logging`, `time`, `subprocess`, `openai`, `httpx`, `fastapi` |
| 26–35 | BabelDoc import | `babeldoc.assets`, `babeldoc.translator.cache`, `babeldoc.format.pdf.high_level.async_translate`, `babeldoc.format.pdf.translation_config.TranslationConfig / WatermarkOutputMode`, `babeldoc.translator.translator.{BaseTranslator, set_translate_rate_limiter}` |
| 37–58 | `_translation_prompt` 辅助函数 | 构造两个翻译器共同使用的标准聊天补全消息列表 |
| 60–104 | `OpenAITranslator` | 两个 LLM 适配器之一 |
| 106–183 | `ClaudeCodeTranslator` | 另一个 LLM 适配器 |
| 187–198 | 模块级常量 | `APP_ROOT`, `UPLOAD_DIR`, `OUTPUT_DIR`, `CONFIG_PATH`, `ALLOWED_*_LANGS`, `WATERMARK_MAP` |
| 200–251 | Logger 配置 | 将 20 个 BabelDoc logger 从 WARNING 提升至 INFO |
| 254–277 | `Task` 类 | 内存内逐任务状态 |
| 281–339 | `AppConfig` 类 | JSON → 类型化配置，附带简单校验 |
| 340–363 | `load_config` / `get_config` / `reload_config` | 异步安全的配置单例 |
| 370–374 | `make_translator` | 工厂函数：`cfg.active_model` → OpenAI 或 ClaudeCode |
| 379–389 | `_file_meta` | 轻量辅助：用于日志行中输出 "{文件名} ({MB} MB)" |
| 391–568 | `run_translation` | 工作协程。逐阶段计时、事件分发、完成/取消/错误处理 |
| 574–582 | `lifespan` | FastAPI 启动/关闭上下文 |
| 585–715 | 路由 | `GET /health`, `POST /api/reload-config`, `POST /api/translate`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel`, `GET /api/tasks/{id}/events` (SSE), `GET /api/tasks/{id}/download/{kind}` |
| 719–897 | `INDEX_HTML` | 三引号字符串，包含整个 HTML/JS 前端 |
| 900–902 | `GET /` 路由 | 返回 `INDEX_HTML` |
| 906–915 | `main()` | `uvicorn.run(...)` 入口 |

---

# 第二篇 — 核心实现

> 真正的代码。每节对应应用的一个逻辑独立的组成部分：配置（§6）被翻译器（§7）消费，翻译器由工作协程（§8）驱动；逐任务状态存放在内存任务表（§9）中；HTTP 路由（§10）和内嵌 HTML（§11）构成对外的接口面。

---

## 6. 配置（`config.json` + `AppConfig`）

### 6.1 配置文件格式

配置文件是一个 JSON 对象，包含四个顶级键：

```json
{
  "active_model": "openai",
  "openai":      { ... },   // active_model == "openai" 时使用
  "claudecode":  { ... },   // active_model == "claudecode" 时使用
  "translation": { ... },
  "server":      { ... }
}
```

`active_model` 为必填。与之对应的引擎配置块为必填，另一个可选。

### 6.2 `openai` 引擎配置块

每个键由 `OpenAITranslator.__init__` 读取：

| 键 | 类型 | 用途 |
|---|---|---|
| `openai_model` | str | 传给 `chat.completions.create` 的 `model=` 参数 |
| `openai_base_url` | str \| null | OpenAI 客户端的 `base_url=` |
| `openai_api_key` | str | `api_key=`；**必填** |
| `openai_timeout` | str \| null | `timeout=`（须能解析为正浮点数） |
| `openai_temperature` | str \| null | **仅在** `openai_send_temprature` 为 `true` 时作为 `temperature=` 发送 |
| `openai_reasoning_effort` | str \| null | **仅在** `openai_send_reasoning_effort` 为 `true` 时发送 |
| `openai_send_temprature` | bool | （拼写错误系有意保留，参见 [issue #175](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/issues/175)） |
| `openai_send_reasoning_effort` | bool | |
| `openai_enable_json_mode` | bool | 供 `request_json_mode` 限速参数使用；纯翻译中基本无效 |

### 6.3 `claudecode` 引擎配置块

| 键 | 类型 | 用途 |
|---|---|---|
| `claude_code_path` | str | `claude` CLI 的可执行文件名称或绝对路径；启动时由 `_test_cli` 校验（执行 `<path> --version`） |
| `claude_code_model` | str | 传给 CLI 的 `--model <model>` 参数 |

### 6.4 `translation` 翻译配置块

| 键 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `qps` | int | `4` | 两重用途：既传给 BabelDoc 作为 `TranslationConfig.qps`（线程池大小），也传给 `set_translate_rate_limiter(...)`（每请求限速） |
| `ignore_cache` | bool | `false` | 转发给 `BaseTranslator.__init__` 作为 `ignore_cache=...` |
| `no_dual` | bool | `false` | `TranslationConfig(no_dual=...)` |
| `no_mono` | bool | `false` | `TranslationConfig(no_mono=...)` |
| `watermark_output_mode` | str | `"watermarked"` | 须为 `WATERMARK_MAP` 的 5 个键之一 |
| `min_text_length` | int | `5` | `TranslationConfig(min_text_length=...)` |

### 6.5 `server` 服务配置块

| 键 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `host` | str | `"0.0.0.0"` | `uvicorn.run(host=...)` |
| `port` | int | `8765` | `uvicorn.run(port=...)` |

### 6.6 配置文件路径

`CONFIG_PATH` 的定义为 `Path(os.environ.get("PDF2ZH_CONFIG", DEFAULT_CONFIG_PATH))`。可以在命令行覆盖：

```bash
PDF2ZH_CONFIG=/etc/pdf-translator/config.json uvicorn serve:app
```

### 6.7 `AppConfig` 类

定义于 `serve.py` 第 281–339 行。`__init__` 中：

1. 读取 `active_model`，校验是否在 `ALLOWED_MODELS` 内。
2. 读取对应的引擎配置块，合并默认值，并执行仅有的两项必填校验：
   - `openai_api_key` 非空。
   - `openai_timeout`（若存在）可通过 `float()` 解析。
3. 读取 `translation.*`，校验 `watermark_output_mode` 是否在 `WATERMARK_MAP` 键集合内。
4. 读取 `server.*`。

`AppConfig.engine` 是一个纯 `dict`（非 Pydantic 模型），通过 `build_settings()`（无参数）延迟转换为 `SimpleNamespace` 后传给 `make_translator(cfg, settings, lang_in, lang_out)`。

### 6.8 热重载

`POST /api/reload-config` 重新执行 `load_config(CONFIG_PATH)` 并替换单例。正在执行的翻译不受影响 —— 它们在 `run_translation` 启动时已通过局部变量捕获了旧的 `AppConfig` 引用。

---

## 7. 翻译器类（`OpenAITranslator`、`ClaudeCodeTranslator`）

两个类均继承自 `babeldoc.translator.translator.BaseTranslator`。BabelDoc 提供了缓存、限速等待、`<think>` 标签剥离及调用计数；**我们仅实现 LLM 调用本身**。

### 7.1 OpenAI 翻译器

位于 `serve.py` 第 60–104 行。

**构造器签名：**

```python
def __init__(self, settings: SimpleNamespace, lang_in: str, lang_out: str)
```

- 调用 `super().__init__(lang_in, lang_out, ignore_cache=settings.ignore_cache)` 设置 `self.lang_in`、`self.lang_out`、`self.cache`、`self.ignore_cache`、`self.translate_call_count` 和 `self.translate_cache_call_count`。
- 从 `settings` 构建 `self.client = openai.OpenAI(base_url=..., api_key=..., timeout=...)`。
- 从 `settings` 读取 `model`、`temperature`、`reasoning_effort`、`enable_json_mode`。每个非默认值通过 `add_cache_impact_parameters(...)` 写入缓存键，使 SQLite 缓存键对（模型、提示词、选项）组合保持唯一。
- `prompt=""` 同样被加入缓存键。

**`do_translate(text, rate_limit_params=None)`：**

```python
return self._call(_translation_prompt(text, self.lang_out))
```

**`do_llm_translate(text, rate_limit_params=None)`：**

调用 `_call` 并传入单个 user 角色的消息以及 `response_format_json=rate_limit_params.get("request_json_mode", False)`。"JSON 模式"路径是 BabelDoc 用于术语抽取的代码路径；在纯翻译场景中 `rate_limit_params` 为 `None`，该路径不生效。

**BabelDoc 仅调用 `do_translate` 和 `do_llm_translate`。** 父类上的包装方法 `translate(...)` / `llm_translate(...)` 供外部代码使用（非我们的代码），会经过 BabelDoc 的缓存 + 限速器。

### 7.2 ClaudeCode 翻译器

位于 `serve.py` 第 106–183 行。

**构造器签名**与 `OpenAITranslator` 相同。

- `super().__init__(lang_in, lang_out, ignore_cache=...)` —— 同上。
- `self.cli_path = settings.claude_code_path`
- `self.model = settings.claude_code_model`
- `_test_cli()` 执行 `[self.cli_path, "--version"]`，若 CLI 缺失则在启动时快速失败。

**`do_translate(text, ...)`：**

1. 构造命令列表：
   ```
   <cli_path> -p
     --model <model>
     --max-turns 1
     --input-format stream-json
     --output-format stream-json
     --verbose
     --disallowedTools <长名单>
   ```
2. 起子进程，向其 stdin 写入单行 JSON `{"type":"user","message":<提示词报文>}`。
3. 读取 stdout，解析每行 `stream-json` 输出，拼接所有 `assistant.message.content[].text` 字段（`_parse_stream_json` 方法）。
4. 若 returncode 非零则抛出 `subprocess.CalledProcessError`；若解析文本为空则抛出 `ValueError("No translation received from Claude Code")`。
5. 从子进程环境变量中移除 `ANTHROPIC_API_KEY`，确保 CLI 使用其自身的认证配置。

**`do_llm_translate`：** 委托给 `do_translate`。CLI 驱动不支持 JSON 模式路径。

### 7.3 公共模式

两个类均：

- **不从** `pdf2zh_next.*` 读取任何类级状态（该包已不在本仓库中 —— 已被删除）。
- 拥有 `name` 属性（`"openai"` 或 `"claudecode"`），长度 ≤ 20 字符（BabelDoc 缓存表列为 `CharField(max_length=20)`）。
- 从父类继承 `prompt(...)`、`add_cache_impact_parameters(...)`、`translate(...)`、`llm_translate(...)`、`_remove_cot_content(...)` 以及缓存。

### 7.4 翻译提示词辅助函数

位于 `serve.py` 第 46–58 行。返回聊天补全的 `messages` 列表：

```python
[{
    "role": "user",
    "content": (
        "You are a professional,authentic machine translation engine.\n\n"
        ";; Treat next line as plain text input and translate it into "
        f"{lang_out}, output translation ONLY. If translation is "
        f"unnecessary (e.g. proper nouns, codes, {{{{1}}}}, etc.), return "
        f"the original text. NO explanations. NO notes. Input:\n\n{text}"
    ),
}]
```

`{{{{1}}}}` 四层花括号是有意为之：经 f-string 求值后字面上变为 `{{1}}`，即 BabelDoc 的公式占位符转义序列。

---

## 8. 翻译工作协程（`run_translation`）

工作协程（`serve.py` 第 391–568 行）是唯一的长时间运行的后台代码路径。其概要如下：

```python
async def run_translation(task: Task, lang_in: str, lang_out: str):
    cfg = await get_config()
    settings = cfg.build_settings()
    translator = make_translator(cfg, settings, lang_in, lang_out)
    term_translator = make_translator(cfg, settings, lang_in, lang_out)
    set_translate_rate_limiter(max(1, cfg.qps))   # BabelDoc 全局

    config = TranslationConfig(... translator=translator,
                               term_extraction_translator=term_translator,
                               lang_in=..., lang_out=...,
                               qps=cfg.qps, pool_max_workers=cfg.qps,
                               watermark_output_mode=...,
                               auto_extract_glossary=False,
                               ...)

    task.status = "running"
    perf: dict[str, float] = {}
    current_stage: str | None = None
    current_stage_t0: float | None = None
    run_t0 = time.monotonic()
    last_pct = -1.0

    try:
        async for event in async_translate(config):
            etype = event["type"]

            # ----- 逐阶段计时 -----
            if etype == "progress_start":
                if current_stage and current_stage_t0:
                    close_stage(perf, current_stage, current_stage_t0)
                current_stage = event["stage"]
                current_stage_t0 = time.monotonic()
                log.info("[%s]   stage START %s (total=%s)", task_id, ...)
            elif etype == "progress_end":
                if current_stage and current_stage_t0:
                    close_stage(perf, current_stage, current_stage_t0)
                    current_stage = None; current_stage_t0 = None
            elif etype == "progress_update":
                pct = int(event.get("overall_progress", 0))
                if pct != last_pct and pct % 5 == 0:
                    log.info("[%s]     progress %s: %d%% ...", task_id, ..., pct)
                    last_pct = pct

            # ----- 分发到 SSE -----
            event_out = {k: v for k, v in event.items() if k != "translate_result"}
            if etype == "finish":
                res = event["translate_result"]
                task.result = { paths..., total_seconds, ... }
                event_out["result"] = task.result
                task.status = "done"
            await task.events.put(event_out)

    except asyncio.CancelledError:
        close_stage(perf, current_stage, current_stage_t0)  # 可能为 None
        log.warning("[%s] translation CANCELLED after %.2fs ...", task_id, ...)
        task.status = "cancelled"
        await emit_perf("cancelled")
        await emit({"type":"cancelled", "task_id": task_id})
        raise
    except Exception as exc:
        log.exception("[%s] translation FAILED after %.2fs in stage=%r",
                      task_id, elapsed, current_stage)
        task.status = "error"
        task.error = str(exc)
        await emit_perf("error")
        await emit({"type":"error", "error": str(exc)})
    else:
        await emit_perf("done")
    finally:
        await task.events.put(None)  # 哨兵
```

### 8.1 关键不变量

1. **`translator` 和 `term_extraction_translator` 是同一个实例。** 这是有意为之 —— 术语抽取已关闭（`auto_extract_glossary=False`），术语翻译器虽被构造但从未被调用。若将来开启术语抽取，两个翻译器都将收到相同的 "Hello" 健康检查。
2. **`qps` 设置传递给三处：**
   - `TranslationConfig(qps=cfg.qps, pool_max_workers=cfg.qps)`
   - 迭代前调用 `set_translate_rate_limiter(max(1, cfg.qps))`
   - 前者控制 BabelDoc 的工作线程池，后者是 `BaseTranslator.translate()` 内部的每请求限速器。
3. **`auto_extract_glossary=False` 为硬编码。** 这是有意为之；基于 LLM 的术语抽取大约会让翻译延迟翻倍。如需变更，修改 `serve.py` 约第 395 行。
4. **`finish` 事件是特例。** BabelDoc 产出该事件时携带 `event["translate_result"]`（一个 `TranslateResult` 数据类，非字典）。我们提取路径字段，在出口处将其重新注入为 `event["result"]`（一个字符串键字典）。
5. **`perf` 事件在三处被发出：** `finish` 时（else 分支）、`CancelledError` 时（关闭已打开的 stage 后）、以及任何其他 `Exception` 时。它携带 `elapsed_total` 和一个以秒数为值的 `stages` 字典。
6. **哨兵 `None`** 用于通知 SSE 消费者停止读取。我们始终在 `finally` 中发送它，因此即便是 Python 层面的异常，SSE 流也会干净关闭。

### 8.2 任务生命周期

```
       POST /api/translate
              │
              ▼
   ┌──────────────────┐
   │  status=pending  │  events=空队列; asyncio_task=None
   │  T0              │
   └────────┬─────────┘
            │  asyncio.create_task(run_translation)
            ▼
   ┌──────────────────┐
   │  status=running  │  events: 陆续收到 {progress_start, progress_update, ...}
   │  T1              │
   └────────┬─────────┘
            │  finish 事件到达
            ▼
   ┌──────────────────┐
   │  status=done     │  events: 收到 finish + perf + None
   │  T2              │  result 已填充
   └──────────────────┘

   （或）转 cancelled → status=cancelled
   （或）转 error    → status=error, error=...
```

`status` 也可以通过 `GET /api/tasks/{id}` 查询（若任务 ID 不在进程内 `TASKS` 字典中，则返回 404）。

---

## 9. 内存任务表与 SSE 队列

### 9.1 Task 类

位于 `serve.py` 第 257–276 行。

| 槽位 | 类型 | 含义 |
|---|---|---|
| `task_id` | `str`（uuid4 hex） | `TASKS` 字典的主键 |
| `pdf_path` | `Path` | 已保存的上传文件位置 |
| `output_dir` | `Path` | BabelDoc 写入翻译后 PDF 的目录 |
| `status` | `str` | `pending` / `running` / `done` / `error` / `cancelled` |
| `events` | `asyncio.Queue[dict]` | 生产者：`run_translation`；消费者：SSE 处理器 |
| `result` | `dict \| None` | `finish` 时填充，携带输出文件路径 |
| `error` | `str \| None` | `error` 时填充 |
| `asyncio_task` | `asyncio.Task \| None` | 取消端点调用 `.cancel()` 的句柄 |

### 9.2 TASKS 字典与 TASK_LOCK

```python
TASKS: dict[str, Task] = {}
TASK_LOCK = asyncio.Lock()
```

`TASK_LOCK` 仅保护 `TASKS[task_id] = task` 插入操作。读取路径（`GET /api/tasks/{id}`、`GET /api/tasks/{id}/events`、`GET /api/tasks/{id}/download/{kind}`、`POST /api/tasks/{id}/cancel`）**不加锁** —— Python 的 `dict[str]` 读取是原子操作，缺失的键由 `TASKS.get(task_id)` 返回 `None`，路由将其转换为 404。

### 9.3 CONFIG 与 CONFIG_LOCK

模式相同，但针对 `AppConfig` 单例。`CONFIG_LOCK` 仅在 `load_config(CONFIG_PATH)` 期间持有，防止并发的 `reload_config` 与首次调用产生竞争。

### 9.4 生命周期

`TASKS` 无上限增长。**无淘汰机制**：每次翻译会在字典中保留到进程退出。重启进程即可清理；磁盘上的 `./outputs/{task_id}/*.pdf` 与内存条目无关。

---

## 10. HTTP 路由

所有路由定义于 `serve.py` 中 `lifespan` 上下文管理器之后。

### 10.1 健康检查

`GET /health`（第 585 行）。

```json
{ "status": "ok", "active_model": "openai" }
```

返回 200 表示进程存活、配置已加载、BabelDoc 缓存已初始化。无认证。

### 10.2 重载配置

`POST /api/reload-config`（第 591 行）。

重新读取 `CONFIG_PATH` 并替换单例。返回新的 `active_model`。幂等，多次调用安全。

### 10.3 提交翻译

`POST /api/translate`（第 597 行）。

表单字段：`file`（PDF 文件）、`lang_in`（源语言）、`lang_out`（目标语言）。

校验（按顺序）：

1. `lang_in ∈ ALLOWED_SOURCE_LANGS` → 否则 422。
2. `lang_out ∈ ALLOWED_TARGET_LANGS` → 否则 422。
3. `file.filename.lower().endswith(".pdf")` → 否则 422。

成功后：

1. 生成 `task_id = uuid.uuid4().hex`。
2. 将上传文件写入 `UPLOAD_DIR / f"{task_id}.pdf"`（使用 `shutil.copyfileobj`）。
3. 创建 `OUTPUT_DIR / task_id /`（BabelDoc 的输出目录）。
4. 在 `TASKS` 中插入 `Task` 对象，并启动 `asyncio.create_task(run_translation(...))`。将任务句柄保存于 `Task.asyncio_task` 供取消使用。
5. 返回 `{"task_id": task_id}`。

### 10.4 查询任务状态

`GET /api/tasks/{task_id}`（第 632 行）。

返回快照：

```json
{
  "task_id": "...",
  "status": "pending|running|done|error|cancelled",
  "result": { ... } | null,
  "error": "..." | null
}
```

`result`（当 `status == "done"` 时）包含：

```json
{
  "original_pdf_path": "/绝对路径/.../uploads/<id>.pdf",
  "mono_pdf_path":     "/绝对路径/.../outputs/<id>/...mono.pdf",
  "dual_pdf_path":     "/绝对路径/.../outputs/<id>/...dual.pdf",
  "no_watermark_mono_pdf_path": "...",
  "no_watermark_dual_pdf_path": "...",
  "auto_extracted_glossary_path": "...",  // 术语抽取关闭时始终为 ""
  "total_seconds": 38.04
}
```

### 10.5 取消任务

`POST /api/tasks/{task_id}/cancel`（第 645 行）。

幂等：

- 任务 ID 未知 → 404。
- 若 `status ∈ {done, error, cancelled}`：返回 `{"task_id":..., "status":..., "already_finished": true}`。**不报错。**
- 否则调用 `task.asyncio_task.cancel()`。返回 `{"task_id":..., "status": "cancelling"}`。

实际的 `status` 仅在 worker 捕获 `asyncio.CancelledError` 并发出 `cancelled` SSE 事件后才变为 `cancelled`。存在一个短暂的时间窗口：HTTP 响应显示 `status: "cancelling"` 而 SSE 流尚未观测到取消 —— 这是无害的。

### 10.6 SSE 事件流

`GET /api/tasks/{task_id}/events`（第 662 行）。

Server-Sent Events 流。Content-Type：`text/event-stream`。Headers：`Cache-Control: no-cache`、`X-Accel-Buffering: no`。

生成器：

```python
while True:
    if await request.is_disconnected():
        break
    try:
        event = await asyncio.wait_for(task.events.get(), timeout=15.0)
    except asyncio.TimeoutError:
        yield ": keepalive\n\n"   # SSE 注释
        continue
    if event is None:            # worker 发出的哨兵
        break
    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
```

15 秒心跳可防止中间代理（nginx、cloudflare）在 BabelDoc 阶段长时间静默期间关闭连接。

### 10.7 下载译文

`GET /api/tasks/{task_id}/download/{kind}`（第 698 行）。

`kind` ∈ `dual` / `mono` / `dual_no_watermark` / `mono_no_watermark`。

状态码：

- 404 —— 任务 ID 未知，或 `kind` 对应的路径在磁盘上不存在（例如配置了 `no_mono=true` 时请求 `mono`）。
- 409 —— 任务尚未完成（`task.result is None`）。
- 422 —— 无效的 `kind`。

成功时：`FileResponse(path, media_type="application/pdf", filename=basename)`。

### 10.8 首页

`GET /`（第 900 行）。

返回 `HTMLResponse(INDEX_HTML)`。HTML 字符串以行内形式定义于第 719–897 行。

**SSE 事件流协议**

每条事件为一行 `data: {json}\n\n`，JSON 至少包含一个 `type` 字段：

| 事件类型 | 附加字段 | 何时发送 |
|---|---|---|
| `progress_start` | `stage`, `stage_progress`, `stage_current`, `stage_total` | 新阶段开始 |
| `progress_update` | `stage`, `stage_progress`, `overall_progress`, `stage_current`, `stage_total` | 当前阶段进展中 |
| `progress_end` | `stage`, `stage_progress`, `stage_total` | 当前阶段完成 |
| `finish` | `result.{original_pdf_path, mono_pdf_path, dual_pdf_path, …, total_seconds}` | 翻译成功 |
| `cancelled` | `task_id` | 用户点击 Cancel |
| `error` | `error` | 翻译失败，任务状态变为 error |
| `perf` | `elapsed_total`, `stages: {name: seconds}`, `reason` | 本服务自行添加的性能汇总事件 |

服务器每 15 秒发送一条 SSE 注释 `: keepalive\n\n` 防止代理关闭空闲连接。

---

## 11. 内嵌 HTML/JS 前端

`INDEX_HTML` 是一个 5 KB 的三引号字符串，包含完整的单页应用：表单、进度、日志、下载区域。全部使用原生 JS（无框架，无转译器）。

### 11.1 页面布局

```
┌────────────────────────────────────────┐
│ PDF Translator                         │
│ Defaults: source English → target ...  │
│                                        │
│ [源语言 ▾]  [目标语言 ▾]                │
│                                        │
│ [选择文件] [Translate] [Cancel]         │ ← Cancel 隐藏，点击后显示
│                                        │
│ ▓▓▓▓▓▓░░░░░░░░░░  35%  Translate Par..  │
│ ┌──── 日志 ──────────────────────────┐   │
│ │ task_id = abc123                  │   │
│ │ event: progress_start             │   │
│ │ ...                                │   │
│ └────────────────────────────────────┘   │
│                                        │
│ [Download Dual]  [Download Mono]  ...   │ ← finish 时出现
└────────────────────────────────────────┘
```

### 11.2 JS 状态机

```
IDLE  ── 点击 Translate ──▶ UPLOADING
                                 │
                                 ▼
                              RUNNING ◀── 点击 Cancel ──┐
                                 │                       │
                          finish 事件                    │
                                 ▼                       │
                               DONE ─────────────────────┘ （重新启用 Translate）
                                 │
                          cancel / error 事件
                                 ▼
                            CANCELLED / ERROR  （同样重新启用 Translate）
```

### 11.3 关键函数

- **`finishClient()`** —— 重新启用 Translate 按钮，隐藏 Cancel 按钮，解绑 Cancel 点击处理函数。**幂等** —— 可在 `es.onerror` 以及 finish / cancelled / error 路径中安全调用。
- **EventSource onmessage** —— 根据 `event.type` 分支：
  - `progress_start` → 设置 `stage` 文本
  - `progress_update` → 更新进度条 + `pct` + `stage`
  - `progress_end` → 显示 "stage done"
  - `finish` → 进度条设为 100，日志显示 "Done in Xs"，追加 4 个下载链接，`es.close()`，调用 `finishClient()`
  - `cancelled` → 记录日志，`es.close()`，调用 `finishClient()`
  - `error` → 在日志中显示 `e.error`，`es.close()`，调用 `finishClient()`
  - `perf` → 日志显示 `perf (reason, total=Xs): stage1=1.20s, ...`
- **EventSource `onerror`** → 日志显示 "SSE 连接关闭"，调用 `finishClient()`（这是浏览器侧的事件，与 `cancelled`/`error` 事件不同）。

---

# 第三篇 — 横切关注点

> 这些话题各自触及第二篇中的多个组件：日志横跨工作协程（§8）、路由（§10）和前端（§11）；取消连接取消路由（§10.5）与工作协程（§8）；错误处理由路由（§10）和工作协程（§8）共享；外部依赖（BabelDoc、`claude` CLI、OpenAI HTTP API、文件系统）支撑第二篇的全部组件。

---

## 12. 日志

### 12.1 两条日志流

1. **服务端 stdout**（uvicorn 的日志处理器）：`log = logging.getLogger("serve")` + 20 个被提升至 INFO 的 BabelDoc logger。
2. **浏览器日志框**（`<pre id="log">`）：SSE `event.type` 流加上 `perf` 汇总。

### 12.2 服务端逐任务日志

每次翻译在 stdout 上输出一条结构化的日志轨迹。以下是基于真实 `async_translate` 运行的示例：

```
[<id>] translation start | engine=openai | model=gpt-4o-mini | en→zh | qps=4 |
        watermark=watermarked | no_dual=False | no_mono=False | file=paper.pdf (2.41 MB)
[<id>]   stage START Parse PDF and Create Intermediate Representation (total=20)
[<id>]   stage END   Parse PDF and Create Intermediate Representation (3.42s, items=20)
[<id>]   stage START Translate Paragraphs (total=145)
[<id>]     progress Translate Paragraphs: 5% (7/145) | stage=Translate Paragraphs
[<id>]     progress Translate Paragraphs: 50% (72/145) | stage=Translate Paragraphs
[<id>]   stage END   Translate Paragraphs (28.71s, items=145)
[<id>]   stage START Save PDF (total=20)
[<id>]   stage END   Save PDF (5.18s, items=20)
[<id>] translation DONE in 38.04s | babeldoc_reported=37.92s
[<id>] stage breakdown: Parse PDF and Create Intermediate Representation=3.42s,
        Translate Paragraphs=28.71s, Save PDF=5.18s
```

取消时：

```
[<id>] translation CANCELLED after 12.30s | partial_stages={'Translate Paragraphs': 12.30}
```

失败时（含调用栈）：

```
[<id>] translation FAILED after 5.12s in stage='Translate Paragraphs': <exc>
Traceback (most recent call last):
  ...
```

### 12.3 被提升到 INFO 的 BabelDoc logger

| Logger | 用途 |
|---|---|
| `babeldoc.format.pdf.document_il.midend.layout_parser` | 逐页版面分析耗时 |
| `babeldoc.format.pdf.document_il.midend.paragraph_finder` | 段落提取 |
| `babeldoc.format.pdf.document_il.midend.il_translator` | 逐段落 LLM 调用 + 缓存命中/未命中 |
| `babeldoc.format.pdf.document_il.midend.il_translator_llm_only` | 纯 LLM 回退路径 |
| `babeldoc.format.pdf.document_il.midend.automatic_term_extractor` | 术语抽取（因已关闭，通常静默） |
| `babeldoc.format.pdf.document_il.backend.pdf_creater` | PDF 写回、字体子集化 |
| `babeldoc.translator.translator` | `_translate_rate_limiter.wait()` 相关日志 |
| `pdf2zh_next.translator`（遗留，可移除） | 逐翻译器调用计数 |

### 12.4 日志格式

```
%(asctime)s [%(levelname)s] %(name)s: %(message)s
```

`asctime` 为本地时间，非 UTC。若需 UTC 以便日志聚合，修改 `logging.basicConfig(format=...)`。

---

## 13. 取消语义

### 13.1 两层取消

1. **用户点击 Cancel** → `POST /api/tasks/{id}/cancel` → `task.asyncio_task.cancel()`。
2. **工作协程在 `async for event in async_translate(config)` 行捕获 `asyncio.CancelledError`**。此时：
   - 关闭当前 `perf` 阶段的计时。
   - 设置 `task.status = "cancelled"`。
   - 向 SSE 队列先后发送 `{"type":"perf", "reason":"cancelled", ...}` 和 `{"type":"cancelled", "task_id":...}`。
   - 重新抛出，使 asyncio 任务以 `CANCELLED` 状态结束。
3. **BabelDoc 本身不会直接感知取消。** 其 `async_translate(...)` 生成器在我们的 `async for` 中被迭代，因此当消费者 (`run_translation`) 在 `await` 中途被取消时，迭代被放弃。内部的 `do_translate` 线程会继续运行，直到下一次 `await task.events.get()` 消费者读取到它的事件（或队列为空，此时工作线程会被原地搁置直至自然结束 —— 可能泄漏几秒 CPU 时间）。

   如需更彻底的取消，可在 Cancel 处理器中调用 `task.config.cancel_translation()`。当前代码未这样做。

### 13.2 取消后保留的内容

- 上传的 PDF 保留在 `./uploads/{task_id}.pdf`。
- 输出目录 `./outputs/{task_id}/` 存在但为空（或包含 BabelDoc 增量写操作的部分内容 —— 部分 PDF 不是有效文档）。
- `TASKS` 条目保留，`status="cancelled"`。

---

## 14. 错误处理

### 14.1 校验错误（4xx）

所有校验错误以 `fastapi.HTTPException(status_code=422, detail=...)` 或 400 形式抛出。浏览器应将此类错误作为用户输入错误处理，并向用户展示 `response.detail`。

| 触发条件 | 状态码 | detail |
|---|---|---|
| `lang_in` 不在白名单中 | 422 | `"lang_in 'X' not in [...]"` |
| `lang_out` 不在白名单中 | 422 | `"lang_out 'X' not in [...]"` |
| 文件非 PDF | 422 | `"file must be a PDF"` |
| 未知任务 ID | 404 | `"task not found"` |
| 下载时任务未完成 | 409 | `"task not finished"` |
| 无效的 `kind` | 422 | `"kind must be one of [...]"` |
| 输出文件不存在（如 `no_mono=true` 时请求 `mono`） | 404 | `"<kind> pdf not produced"` |

### 14.2 翻译错误（等效 5xx）

工作协程**不会向上抛出**；它捕获异常，通过 `log.exception(...)` 记录，并发送 `{"type":"error","error": str(exc)}` SSE 事件。浏览器记录错误消息并重新启用 Translate 按钮。HTTP `GET /api/tasks/{id}` 返回 `status="error"`，`error` 字段携带异常消息。

### 14.3 启动错误

若 `load_config(CONFIG_PATH)` 抛出异常（如缺少 `openai_api_key`），`lifespan` 启动失败，uvicorn 以非零状态退出。错误信息出现在 uvicorn 日志中；HTTP 服务器不会启动。

### 14.4 ClaudeCode 子进程错误

`subprocess.CalledProcessError` 和 `subprocess.TimeoutExpired` **不会**在 `do_translate` 内部被捕获 —— 它们沿 `async_translate` → `run_translation` → worker handler 路径向上传播，worker handler 捕获所有 `Exception` 并发出 `error` 事件。

---

## 15. 外部依赖

### 15.1 PyPI 包

在 `pyproject.toml` 中声明。

| 包 | 用途 | 备注 |
|---|---|---|
| `fastapi` | HTTP 框架 | 路由、`UploadFile`、`HTTPException`、`FileResponse` |
| `uvicorn[standard]` | ASGI 服务器 | 入口 |
| `python-multipart` | `multipart/form-data` 解析 | `File(...)` / `Form(...)` 所必需 |
| `babeldoc` | PDF 解析、版面分析、翻译驱动、BaseTranslator | `>=0.6.2,<0.7.0` |
| `openai` | 聊天补全 | `OpenAITranslator` 使用 |
| `httpx` | `openai` 的底层 HTTP 客户端 | 传递依赖 |
| `pymupdf` | PDF 渲染等 | 由 BabelDoc 带入 |

Pydantic 作为 BabelDoc 的传递依赖存在，但本代码库**不直接使用**。

### 15.2 外部 CLI / 服务

| 服务 | 使用方 | 失效模式 |
|---|---|---|
| `claude` CLI | `ClaudeCodeTranslator` | `_test_cli` 在翻译器构造时抛出异常；FastAPI 启动可完成，但 `/api/translate` 将返回 500（因为 worker handler 捕获了异常） |
| OpenAI 兼容 HTTP API | `OpenAITranslator` | 网络/HTTP 错误由 BabelDoc 的重试逻辑捕获；非可重试错误向上传播为 500 |

### 15.3 文件系统

- `./uploads/{task_id}.pdf` —— 由 `POST /api/translate` 写入，BabelDoc 读取。**永久保留**（按设计）。
- `./outputs/{task_id}/*.pdf` —— 由 BabelDoc 写入。**永久保留**。
- `~/.cache/pdf2zh_next/cache.v1.db` —— SQLite 翻译缓存。BabelDoc 自身的清理逻辑会自动将其裁剪至 50,000 行。
- `./uploads/` 和 `./outputs/` 在首次启动时由 `lifespan` 上下文管理器创建。

---

# 第四篇 — 运维与扩展

> 供运行、调试和扩展服务的人员阅读。§16 是新增功能的菜谱式指南；§17 是按症状查找对照表；§18 是安装与运行参考；§19 是将全文串联起来的术语表。

---

## 16. 扩展指南

### 16.1 新增 LLM 引擎

1. 在 `USER_GUIDE.md` 第 3 节的 `config.json` schema 文档中追加新的引擎配置块。
2. 在 `serve.py` 的现有翻译器之间追加一个新类，继承 `BaseTranslator`。实现 `__init__(settings, lang_in, lang_out)`、`do_translate(text, rate_limit_params=None)`、`do_llm_translate(text, rate_limit_params=None)`。模式：复制 `OpenAITranslator`，替换客户端 + 调用。
3. 更新 `AppConfig.__init__` 以识别新的 `active_model` 值并解析对应的引擎配置块。
4. 更新 `make_translator` 以分派到新类。
5. 更新 `ALLOWED_MODELS` 常量。
6. 重新构建并重新测试。

### 16.2 新增 UI 语言

`INDEX_HTML` 中的 HTML 是手工编写的英文界面。本地化步骤：

1. 将 `INDEX_HTML` 复制为 `index_<语言>.html`。
2. 翻译字符串。
3. 增加一个 `GET /<lang>` 路由返回本地化后的 HTML。
4. 在页面上增加语言选择器（当前不存在 —— UI 仅英文）。

关于 BabelDoc 语言代码，参见 §6.4 以及 `ALLOWED_SOURCE_LANGS` / `ALLOWED_TARGET_LANGS`。若要增加一个 BabelDoc 支持但 UI 不包含的语言代码，只需向相应元组追加即可。

### 16.3 开启术语抽取

在 `run_translation` 的 `TranslationConfig(...)` 调用中（约第 395 行），将 `auto_extract_glossary=False` 改为 `True`。长文档的翻译时间大约会翻倍。

若还希望使用**独立的术语抽取引擎**，将 `term_translator = make_translator(...)` 一行替换为不同的引擎配置块。需要在 `config.json` 中增加第二个引擎配置段，并增加第二次 `make_translator` 调用。

### 16.4 持久化 TASKS 到磁盘

`TASKS` 目前是纯内存。一种简单的增强方案：

1. 在 `Task` 创建时，向 `./outputs/{task_id}/manifest.json` 写入一份 JSON 清单。
2. 启动时扫描 `./outputs/*/manifest.json` 并重新填充 `TASKS`。
3. 在取消端点中同样移除 manifest。

如此可在进程重启后存活。旧任务的内存 `events` 队列无法重建 —— 旧任务对后续的 `/api/tasks/{id}/events` 调用应报告 `status="unknown"`。

### 16.5 切换 gunicorn / 多 worker

`uvicorn.run(...)` 默认单进程。生产环境可切换为：

```bash
gunicorn serve:app -k uvicorn.workers.UvicornWorker -w 2 --bind 0.0.0.0:8765
```

⚠️ 当 `w > 1` 时，`TASKS` 是**每进程独立**的 —— 提交到 worker A 的翻译无法从 worker B 取消。解决方案：

- 固定 `w=1`，或
- 将 `TASKS` 和 `CONFIG` 替换为 Redis 存储。

---

## 17. 已知问题排查

| # | 现象 | 原因 | 修复方法 |
|---|---|---|---|
| 1 | `ValidationError: translate_engine_type Input should be 'OpenAI'` | 将 `openai.translate_engine_type` 设置为模型名称（如 `"MiniMax"`、`"DeepSeek"`） | 此字段为**引擎类标识符**，非模型名。保持为 `"OpenAI"`。将模型名写入 `openai.openai_model`，将服务商地址写入 `openai.openai_base_url` |
| 2 | `openai_timeout Input should be a valid string` | 设置为 `"openai_timeout": 60`（整数） | 使用 `"openai_timeout": "60"`（字符串） |
| 3 | `claude CLI not found at 'claude'` | `claude` 二进制文件不在 `$PATH` 上 | 安装 Claude Code，或将 `claudecode.claude_code_path` 设为绝对路径 |
| 4 | `No module named 'babeldoc'` | 当前 Python 环境中未安装 `babeldoc` | 在仓库根目录执行 `pip install -e .` 或 `pip install 'babeldoc>=0.6.2,<0.7.0'` |
| 5 | Cancel 后 SSE 流挂起 | 浏览器缓存了 EventSource；`es.close()` 是正确的，但服务端生成器仅在 worker 发出哨兵后才退出 | 检查 `run_translation` 的 `finally` 块中是否存在 `await task.events.put(None)`；若缺失，消费者生成器永远不会退出 |
| 6 | `lang_out=xx` 返回 422 | `ALLOWED_TARGET_LANGS` 白名单是硬编码的 | 将语言代码添加到 `ALLOWED_TARGET_LANGS` 并在 `INDEX_HTML` 的 `<select>` 选项中追加 |
| 7 | 第一次翻译任何 PDF 都很慢 | BabelDoc 在首次运行时预热版面模型、下载字体等 | 等待。预热每 Python 进程仅一次；后续同一或不同 PDF 的翻译速度正常 |
| 8 | output 永不删除 | 按设计 —— `outputs/` 和 `uploads/` 永久保留 | 手动清理：`rm -rf outputs/* uploads/*` |
| 9 | worker 中抛出 `RuntimeError`，`term_translator` 为 None | 上游 BabelDoc 在 `term_extraction_translator=None` 时的一处 bug | 当前代码将两个 translator 传为同一实例；不会触发此情况。若设置 `term_extraction_translator=None`，BabelDoc 自身会使用 `translator` 作为回退，因此仍应正常工作 |

---

## 18. 构建、运行、部署

### 18.1 安装

以 conda 为例：

```bash
conda create -n pdf-translator python=3.12 -y
conda activate pdf-translator
pip install -e .
# 或仅安装依赖：
pip install fastapi 'uvicorn[standard]' python-multipart \
            'babeldoc>=0.6.2,<0.7.0' openai
```

### 18.2 配置

编辑仓库根目录下的 `config.json`。最低配置：

```json
{
  "active_model": "openai",
  "openai": {
    "translate_engine_type": "OpenAI",
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "sk-...",
    "openai_timeout": "60"
  }
}
```

### 18.3 运行

```bash
# 开发环境：
uvicorn serve:app --reload --port 8765
# 生产环境：
uvicorn serve:app --host 0.0.0.0 --port 8765 --workers 1
```

在浏览器中打开 <http://localhost:8765/>。

### 18.4 Docker

仓库根目录的 `Dockerfile` 安装项目并启动 uvicorn。对于本项目的用途，该文件可直接使用，因为我们未添加新的系统依赖。上游的 `Dockerfile.China` 和 `Dockerfile.Demo` 原位于 `script/` 目录中，但 `script/` 已在清理时被删除；如需恢复，从 git 历史中提取。

### 18.5 日志查看

- 使用 `tail -f` 跟踪 uvicorn stdout 查看逐阶段耗时日志行。
- 打开浏览器的 `<pre id="log">` 区域查看实时到达的 SSE 事件。
- 如需更深入的 BabelDoc 内部日志，可设置环境变量 `LOGLEVEL=DEBUG`（尚未实现，需对 `logging.basicConfig` 做小改动）。

---

## 19. 术语表

| 术语 | 含义 |
|---|---|
| **BabelDoc** | 上游 Python 库，承担实际的 PDF 解析与翻译编排 |
| **TranslationConfig** | BabelDoc 的数据类，捆绑输入文件、输出目录、翻译器、语言代码及 QPS。由 `run_translation` 构造 |
| **`async_translate`** | BabelDoc 的异步生成器，产出 `progress_*` 和 `finish` 事件 |
| **BaseTranslator** | BabelDoc 的抽象类，处理缓存 + 限速等待 + `<think>` 标签剥离。`OpenAITranslator` 和 `ClaudeCodeTranslator` 均继承自它 |
| **Task** | 本服务的逐翻译状态对象：输入/输出路径、状态、事件队列、asyncio 任务句柄 |
| **SSE** | Server-Sent Events（服务器推送事件），`/api/tasks/{id}/events` 使用的 `text/event-stream` 协议 |
| **dual PDF** | 双语 PDF：原始文本与翻译文本在同一页上（并排或堆叠，取决于版面） |
| **mono PDF** | 单语 PDF，仅包含翻译文本 |
| **qps** | Queries Per Second（每秒请求数）。在本代码库中有两重用途：作为 BabelDoc 的工作线程池大小，以及作为每请求限速等待间隔 |
| **术语抽取（term extraction）** | BabelDoc 的一项功能，扫描文档提取领域专用术语并构建词汇表。当前**默认关闭** |
| **stage（阶段）** | BabelDoc 内部阶段：`Parse PDF and Create Intermediate Representation`、`Detect Scanned File`、`Translate Paragraphs`、`Save PDF` 等。每个阶段会发出 `progress_start` 和 `progress_end` 事件 |
| **perf 事件** | 本服务自行添加的 SSE 事件（`{"type":"perf", ...}`），携带 `{阶段名称: 耗时秒数}` 分解 |

---

*全文完。应用代码总计 915 行，单文件 `serve.py`。应用文档：本文档 + `USER_GUIDE.md` + `PLAN.md` + `requirement-prompts.md`。*
