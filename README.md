# 玩局 桌游推荐集成版

EE6405 课程项目。在团队已有的双语问卷、推荐、规则讲解与 SQLite 目录上，增加可追踪的模型调用、补充偏好与换一批、第一局带玩和来源标注。本次交付在本地使用，打开网页即可操作，无需账号。网页和 API 使用一个 Python 服务，无需前端构建或 Docker。默认策略由负责人指定为 `s_team / t1 / g2`。

## 项目结构

```text
backend/          兼容团队 API；模型配置、harness、推荐、带玩、问答与用量
frontend/
  en/、zh/        双语页面，扩展问卷、带玩与设置
  shared/         原生 JavaScript 和 CSS
data/
  database/       已构建的目录 SQLite 与 schema.sql（提交）
  sourcedata/     原始 JSON、Excel 扩展、英文补充翻译与规则书（提交）
  app.db          会话、缓存、登录令牌与用量（运行时生成，不提交）
  traces/         调用记录（不提交）
prompts/runtime/  产品运行时提示词与工具说明
tests/            产品回归测试；fixtures/mock_auto 也是离线演示必需资源
scripts/          账号添加与数据维护
docs/             架构、决策、演示与冷启动验收
start.py          启动前端和后端
.env.example      产品配置模板，无真实密钥
```

## 启动

需要 Python 3.11–3.14。以下命令均在仓库根目录执行，首次在自己的虚拟环境中安装依赖：

```sh
git clone https://github.com/KeHuang2004/AI-Powered-Board-Game-Recommendation-and-Interactive-Assistant-.git
cd AI-Powered-Board-Game-Recommendation-and-Interactive-Assistant-
# 产品改动合入 main 前，切换到收到的产品交付分支或 v1.0 tag。
python -m venv .venv
```

macOS / Linux：

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

如果 PowerShell 不允许激活，可直接用 `.venv\Scripts\python.exe` 执行上述 pip 及后续 Python 命令。团队原有 Windows Python 3.14 的 `.runtime` 离线依赖查找逻辑保留；新的克隆请先按上面安装依赖。

### 本地使用

保持 `.env` 中 `DEPLOY_MODE=local`，填入模型配置，执行：

```sh
python start.py
```

服务就绪后会自动打开英文首页 <http://127.0.0.1:8001/en/>。手动访问根地址也会进入 `/en/`，可从页头 Settings 中切换到中文。模型、密钥和联网搜索在本机 `.env` 中配置，修改后重启服务。模型名留空时可以看目录推荐和规则，带玩需模型配置就绪。

**无需密钥的离线演示**使用 `.env` 中的 `MODEL_PROVIDER=mock`，其余模型字段留空。启动即可完成问卷、推荐、详情和带玩。如需展示搜索工具和来源卡片，同时设置 `SEARCH_PROVIDER=mock`。离线演示使用固定回复和示例网页。

```sh
python start.py --port 8002 --no-browser
```

上例更换端口且不自动开浏览器。缺少 `data/database/boardgames.sqlite3` 时会提示 `python -m backend.build_catalog`；仓库已提交该目录库，正常克隆无需重建。接口文档在 `/docs`，`/health` 显示模式和配置状态，实际连接可用性以测试结果为准。

## 全局模型配置

推荐排序、AI 规则讲解与带玩共用根目录 `.env`。所有产品配置及中文注释见 [.env.example](.env.example)。模型名不设默认值，由使用者按已开通的服务填写：

```dotenv
MODEL_PROVIDER=compatible
MODEL_NAME=
MODEL_API_KEY=
MODEL_BASE_URL=
```

| `MODEL_PROVIDER` | 配置 |
|---|---|
| `deepseek` | 填 `MODEL_API_KEY`、`MODEL_NAME`，使用官方接口地址。 |
| `compatible` | 填 `MODEL_API_KEY`、`MODEL_NAME`、`MODEL_BASE_URL`；地址保留服务要求的前缀，例如 `/v1`。须支持 Chat Completions 工具调用。 |
| `gemini` | 填 `MODEL_API_KEY`、`MODEL_NAME`，经官方 OpenAI 兼容接口调用。 |
| `catalog` | 仅使用目录推荐和资料库规则，不支持带玩。 |
| `mock` | 固定离线响应，无密钥，无外网模型调用，可演示完整流程。 |

`MODEL_REASONING_EFFORT`、`MODEL_EXTRA_BODY` 依服务支持选填。旧的 `RECOMMEND_*`、`GEMINI_MODEL`、`GEMINI_API_KEY`、`DEEPSEEK_API_KEY` 仍兼容；新配置建议只使用 `MODEL_*`，显式存在的全局字段（即使为空）优先于旧字段。手工修改 `.env` 后重启。

搜索默认关闭。`SEARCH_PROVIDER=deepseek` 时，优先用 `SEARCH_API_KEY`，其次尝试模型密钥或旧 `DEEPSEEK_API_KEY`。搜索用于带玩中缺少规则书的游戏，由 harness 调用搜索和取网页工具。本次冷启动只验证 mock 搜索；真实搜索服务可用性未经本次验收。

## 推荐和规则

- 问卷沿用团队的实际人数、主持、道具、场合与氛围，新增第 6 项选填补充偏好。
- `POST /recommend` 默认使用保留团队排序与提示词的 `s_team`，先过滤人数、主持及专用道具，再在合格候选中推荐最多 3 款；模型出错可回落到目录推荐。
- 双语名称、标签与规则按页面语言读取；详情保留目录规则，同时经 harness 生成 AI 新手讲解。资料库规则在请求失败时仍可阅读。
- `POST /recommend` 仍接受 `language: "en" | "zh"`，省略时返回中文。新增 `/recommend/refine` 支持补充偏好并排除已展示游戏。
- KK 的 `GET /games`、`GET /games/{game_id}/core-rules` 和 `POST /rules/explain` 继续保留。
- 详情中「开始带玩」→ 填或跳过名字 → 分步推进第一局；计划、对话、来源标签、骰子、计分和计时工具共用会话状态。`t1` 的「下一步」由代码推进，不调用模型。

## 数据目录与更新

```text
data/database/boardgames.sqlite3       目录数据库，与用户运行数据分开
data/database/schema.sql              保留团队原表与字段，增加 extra_fields、rulebooks
data/sourcedata/boardgame_dataset.json 英文原始数据
data/sourcedata/桌游数据集_中文版.json   中文原始数据
data/sourcedata/game_ids.json          固定中英文游戏 ID
data/sourcedata/translations.en.json   扩展字段的英文翻译
data/sourcedata/rulebooks/             已导入的规则书 PDF
```

应用启动时从 SQLite 加载目录快照。团队的 `games`、`game_text`、`game_player_counts`、`game_tags` 保留；扩展字段和规则书来自补充资料。用户会话及缓存写入另一个自动创建的 `data/app.db`，无需独立数据库服务。

更新源数据时保留已有游戏 ID、双语配对与字段约束，在项目根目录执行 `python -m backend.build_catalog` 并重启。构建器校验双语、人数、唯一键、外键和扩展字段；校验失败保留上次成功的目录库。Excel 补充文件、规则书 PDF 和 `translations.en.json` 也应随产品源码一起保留。

## Agent harness branch

产品改动直接基于[团队仓库](https://github.com/KeHuang2004/AI-Powered-Board-Game-Recommendation-and-Interactive-Assistant-)的 `main`（基点 `fe1084d`）开发，保留原页面、接口和团队推荐基线。`backend/model_api.py` 原文件保留，新调用走 harness。合入时需要完整的产品改动，包含运行时模板、mock fixtures、数据扩展及文档，无需另建应用。

产品测试：`python -m pip install -r requirements-dev.txt`，然后 `python -m pytest` 和 `ruff check backend tests`。测试使用 mock，不消耗真实模型额度。交付验收见 [coldstart.md](docs/coldstart.md)，实现说明见 [architecture.md](docs/architecture.md)、[decisions.md](docs/decisions.md)，演示步骤见 [demo_flow.md](docs/demo_flow.md)。

不要提交 `.env`、`accounts.yaml`、`data/app.db`、`data/traces/` 或虚拟环境。
