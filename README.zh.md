# 玩局 · 桌游推荐与带玩助手

[English](README.md) · **简体中文**

玩局是 EE6405 的小组课程项目。它帮助玩家按聚会条件挑选桌游、了解规则，并跟着助手开始第一局。项目内置 **235 款中英文桌游资料**，网页和 API 由同一个 FastAPI 服务在本机提供。

网站启动后可直接使用，无需登录。

## 可以做什么

| 功能 | 使用方式 |
| --- | --- |
| 找桌游 | 填写实际到场人数、主持人、道具、场合和氛围，也可以补充自由偏好。系统先筛选可玩人数、主持和道具条件，再排序并返回最多三款。 |
| 换一批 | 补充偏好或按已提交偏好换一批。系统沿用原问卷，并排除这轮推荐链里已经展示的游戏。 |
| 看规则 | 详情页直接显示目录里的核心规则和详细规则。配置模型后，页面还会请求基于目录规则的 AI 新手讲解。 |
| 开始带玩 | 填写玩家名字或使用默认称呼，按计划推进第一局。带玩页支持规则问答、骰子、计分和计时。 |
| 参与贡献 | 点击右上角「参与贡献」，打开 GitHub 仓库或反馈问题、规则纠错和游戏建议。 |

## 快速启动

使用 **Python 3.11–3.14**。在包含此 README 的项目根目录执行命令。仓库已包含 SQLite 目录库，可以直接启动。

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果已经有 Conda 环境，激活后用该环境的 `python` 代替 `.\.venv\Scripts\python.exe` 即可。

**macOS 或 Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

首次运行如果想先用无密钥模式，请在根目录 `.env` 里修改这三项：

```dotenv
MODEL_PROVIDER=mock
SEARCH_PROVIDER=none
```

在已激活的环境中运行 `python start.py`，或在 PowerShell 里运行 `.\.venv\Scripts\python.exe start.py`。服务默认监听 `127.0.0.1:8001`，启动后自动打开英文首页。可以通过右上角 **Settings** 切换语言，也可以直接访问：

| 页面 | 地址 |
| --- | --- |
| English | [127.0.0.1:8001/en/](http://127.0.0.1:8001/en/) |
| 中文 | [127.0.0.1:8001/zh/](http://127.0.0.1:8001/zh/) |
| 健康检查 | [127.0.0.1:8001/health](http://127.0.0.1:8001/health) |
| OpenAPI 文档 | [127.0.0.1:8001/docs](http://127.0.0.1:8001/docs) |

如果 8001 已被占用，运行 `python start.py --port 8002 --no-browser`，访问地址也改用 8002。停止服务请按 **Ctrl+C**。

## 全局模型配置

推荐、AI 规则讲解和 Agent Harness 带玩都读取**项目根目录的 `.env`**。已在进程中设置的环境变量优先。编辑 `.env` 后重启服务。

| `MODEL_PROVIDER` | 配置和行为 |
| --- | --- |
| `mock` | 使用本地固定回复，无需模型密钥，可以演示完整流程。 |
| `catalog` | 使用目录排序和已存规则，AI 讲解、问答和带玩不可用。 |
| `deepseek` | 填写 `MODEL_NAME` 和 `MODEL_API_KEY`，服务地址由后端提供。 |
| `gemini` | 填写 `MODEL_NAME` 和 `MODEL_API_KEY`，通过 Gemini 的 OpenAI 兼容接口调用。 |
| `compatible` | 填写 `MODEL_NAME`、`MODEL_API_KEY` 和 `MODEL_BASE_URL`。保留接口所需的路径前缀，例如 `/v1`。带玩需要支持工具调用的 Chat Completions 接口。 |

使用真实模型时，请填写账号已开通的模型名，并将密钥填在本机的 `.env`。项目兼容 `DEEPSEEK_API_KEY`、`GEMINI_API_KEY` 和 `RECOMMEND_*` 等旧字段。在同一配置来源中，显式设置的 `MODEL_*` 优先于对应的旧字段，空值也算显式设置。可用选项集中在根目录的 `.env`。以 `##` 开头的可选项使用程序默认值，启用时去掉行首注释。

**联网搜索独立配置。** `SEARCH_PROVIDER=none` 关闭新的搜索请求。`SEARCH_PROVIDER=mock` 用示例网页演示搜索和来源卡片。`SEARCH_PROVIDER=deepseek` 需要账号可用的搜索服务，优先使用 `SEARCH_API_KEY`，其次尝试 `MODEL_API_KEY` 或旧的 `DEEPSEEK_API_KEY`。

## 系统如何运行

1. 前端把问卷发给 FastAPI。默认策略 `s_team` 先筛选目录，再根据标签和推荐人数排序，最多取 12 个候选交给模型重排。模型失败时可以使用目录排序，继续根据问卷中的结构化条件给出结果。
2. 详情页从目录读取当前语言的规则。AI 新手讲解通过独立请求生成，用户等待期间仍能阅读已存规则。
3. 默认 `t1` 模式下，Agent Harness 生成或复用结构化带玩计划，代码负责步骤推进和会话状态。新的聊天消息可以调用配置的模型，并执行当前阶段开放的工具。来源徽标显示回答所标注的来源类别。
4. 会话和缓存保存在运行时 SQLite。模型调用和工具事件写入本地追踪记录。

| 路径 | 职责 |
| --- | --- |
| [frontend/](frontend/) | 英文和中文页面，共用 JavaScript 和 CSS |
| [backend/setting.py](backend/setting.py) | 读取 `.env`，提供模型和搜索设置接口 |
| [backend/llm.py](backend/llm.py) | 模型客户端、供应商接口适配和 Mock 回复 |
| [backend/server.py](backend/server.py) | FastAPI 入口、路由注册及带玩接口 |
| [backend/database.py](backend/database.py) | 构建和读取目录、连接运行库，并用 BM25 检索规则书 |
| [recommend_service.py](backend/recommend/recommend_service.py) | 游戏推荐与换一批接口 |
| [quick_guide_service.py](backend/quickguide/quick_guide_service.py) | 目录规则和 AI 快速指南接口 |
| [backend/harness/](backend/harness/) 和 [backend/tutor/](backend/tutor/) | 模型和工具编排、带玩计划、会话状态及规则问答 |
| [prompts/runtime/](prompts/runtime/) | 提示词模板与工具定义 |

### 提示词目录

```text
prompts/runtime/
  recommend/          推荐排序、请求和输出示例
  quickguide/         规则讲解提示词和讲解风格
  agent_harness/
    tutor/            首局计划、对话和规则书上下文
    probe/            模型能力探测
  shared/             工具定义、校验文案和文本工具协议
```

加载器按文件名在这些目录中查找模板，因此不同子目录的模板文件名也需唯一。

## 数据与 SQLite

- [`data/database/boardgames.sqlite3`](data/database/boardgames.sqlite3) 是启动时加载的桌游目录。[表结构](data/database/schema.sql) 包含游戏公共字段、双语文本、实际可玩与推荐人数、标签、扩展字段和规则书。
- [`data/sourcedata/`](data/sourcedata/) 包含中英文 JSON、扩展字段 Excel、双语游戏 ID 对照表、英文扩展翻译，以及 UNO、Blackjack 和 Texas Hold’em 的三份 PDF。
- `data/app.db` 由系统运行时生成，用于保存推荐记录、带玩会话、缓存和用量。`data/traces/` 保存模型调用追踪。

问卷接受 **1–21 人**。当前目录里《血染钟楼》支持的实际人数最高，为 **21 人**。《王国制图师》在本目录中设为 **2–15 人**。实际可玩人数用于筛选，推荐人数用于候选排序。本次新增的 60 款均有中英文核心规则和详细规则，开局用品、胜负关系等英文扩展字段仍待翻译。

更新目录时，保留已有游戏 ID，在 `game_ids.json` 为每款新游戏追加唯一 ID 和中英文名称，并同步更新两份 JSON 和 Excel。英文扩展字段按游戏 ID 补入 `translations.en.json`，然后运行：

```bash
python -m backend.database
```

导入程序在替换目录库前会检查双语名称、共用字段、人数、Excel 标签和数据库完整性。重建成功后重启应用，即可加载新版数据库。

## API 参考

完整请求格式见 [OpenAPI 文档](http://127.0.0.1:8001/docs)。主要接口如下。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 返回加载的游戏数、配置状态和当前策略 |
| `POST /recommend` | 返回最多三款游戏及本次推荐 ID（`rid`） |
| `POST /recommend/refine` | 用 `rid` 和可选的 `message` 换一批 |
| `GET /games` 和 `GET /games/{game_id}/core-rules` | 列出游戏及读取目录核心规则 |
| `GET /api/games/{game_id}?lang=zh` | 读取指定语言的详情和规则书可用状态 |
| `POST /rules/explain` | 用 `mode: "llm"` 生成讲解，或用 `mode: "raw"` 读取目录规则 |
| `POST /api/sessions` 和 `POST /api/sessions/{session_id}/message` | 创建带玩会话或发送消息，使用服务端事件流（SSE） |
| `GET /api/sessions/{session_id}` | 恢复会话状态和聊天记录 |
| `POST /api/sessions/{session_id}/next`、`/jump` 或 `/tools/{name}` | 使用同一会话路径前缀推进步骤、跳转阶段或执行工具 |

`POST /recommend` 请求体示例：

```json
{
  "players": 4,
  "has_host": false,
  "has_dedicated_components": false,
  "scene": "friends",
  "mood": "relaxed",
  "language": "zh",
  "free_text": "我们第一次玩。"
}
```

## 演示与排查

1. 打开 `/health`，确认 `games_loaded` 为 **235**。
2. 试填 4 人、无人主持、只用通用用品、朋友聚会、轻松聊天，检查结果页显示的推荐来源。
3. 打开一款游戏，查看目录规则和 AI 新手讲解，再点「开始带玩」并跳过玩家名字。
4. 点一次「下一步」并发一条新问题。即使带玩计划命中缓存，新的聊天消息也可以检查当前模型调用。

如果推荐回到目录排序或讲解失败，检查 `.env` 中的供应商、模型名和密钥，然后重启。`/health` 可以查看配置状态，实际调用是否成功以返回的讲解或聊天回复为准。如果演示时需要重新生成计划，可临时设置 `PLAN_CACHE=0` 并重启。Mock 使用固定内容演示流程，真实模型的回答和引用质量需要单独检查。

## 开发与贡献

```bash
python -m pip install -r requirements.txt pytest ruff
python -m pytest -q
ruff check backend tests start.py
```

自动测试使用模拟模型。

如果修改数据，请把对应源文件和重建后的目录库放在同一个 Pull Request 中。问题反馈和代码修改可以通过 [GitHub 仓库](https://github.com/KeHuang2004/AI-Powered-Board-Game-Recommendation-and-Interactive-Assistant-) 提出。
