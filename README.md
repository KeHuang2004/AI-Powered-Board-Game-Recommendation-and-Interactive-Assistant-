# 玩局 桌游推荐集成版

## 项目结构

```text
backend/   推荐、规则 API、模型调用、SQLite 读取与数据导入
frontend/
  en/      英文页面
  zh/      中文页面
  shared/  两种语言共用的 JavaScript 和 CSS
data/      SQLite 数据库、表结构和原始 JSON
start.py   启动前端和后端
```

模型配置 `.env` 和依赖清单 `requirements.txt` 位于项目根目录。

## 启动

在本目录打开终端并运行：

```powershell
python start.py
```

打开 <http://127.0.0.1:8001/>。网页与 API 在同一个地址，接口文档在 <http://127.0.0.1:8001/docs>。
访问首页会进入 <http://127.0.0.1:8001/en/>，点击页头「中文」可进入 <http://127.0.0.1:8001/zh/>。两种语言共用同一个服务和数据库。
在这台 Windows 电脑上，Python 3.14 可以使用本目录里预先准备的离线依赖。把源码复制到其他电脑时，可在自己的虚拟环境中运行 `python -m pip install -r requirements.txt`，然后启动。

## 全局模型配置

推荐排序和 AI 规则讲解共用项目目录中的 `.env`。用 `notepad .env` 打开，DeepSeek 配置如下，密钥填在等号后。修改后重启 `python start.py`。

```dotenv
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-flash
DEEPSEEK_API_KEY=
```

| `MODEL_PROVIDER` | 配置 |
| --- | --- |
| `deepseek` | 使用官方 DeepSeek API，填写 `DEEPSEEK_API_KEY`，可通过 `MODEL_NAME` 换模型。 |
| `compatible` | 使用支持 Chat Completions 和 JSON 对象输出的服务，填写 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME`。基础地址保留服务要求的路径前缀，例如 `/v1`。 |
| `gemini` | 填写 `GEMINI_API_KEY`，通过 `MODEL_NAME` 选择模型。 |
| `catalog` | 使用本地推荐和资料库规则。 |

旧的 `RECOMMEND_*` 配置仍可读取。填写全局字段后以全局配置为准。密钥只保存在本地服务端。访问 `/health` 可查看共用模型配置是否就绪，实际调用结果以推荐来源和详情页讲解为准。

## 推荐和规则

- 问卷收集实际到场人数、是否有主持人、可用道具、场合和氛围。
- `POST /recommend` 先筛选可玩人数和主持、组件条件，再按氛围、场合、推荐人数和等级排序。配置的模型可以在前 12 个合格候选中重排，最多选 3 款。
- 结果中的游戏名、标签、规则和预写推荐理由按页面语言从数据库读取，模型生成的理由也使用该语言。打开详情后自动生成当前语言的分步讲解。资料库规则会立即显示，模型请求失败时仍可阅读。
- `POST /recommend` 接受 `language: "en" | "zh"`，旧客户端省略时仍返回中文。两种语言使用相同的硬条件和本地初排。
- KK 的 `GET /games`、`GET /games/{game_id}/core-rules` 和 `POST /rules/explain` 可继续使用。

## 数据目录与更新

```text
data/
  database/
    boardgames.sqlite3         应用运行时读取的 SQLite 数据库
    schema.sql                 数据表定义和约束
  sourcedata/
    boardgame_dataset.json     英文原始数据
    桌游数据集_中文版.json        中文原始数据
    game_ids.json              固定的游戏 ID 和中英文名称映射
```

应用启动时从 SQLite 加载目录到内存，推荐和规则讲解使用同一份快照。原始 JSON 只用于导入，运行时不读取。SQLite 由 Python 标准库支持，无需启动独立数据库服务。

- `games` 保存共享的游戏 ID、主持人要求、道具要求和推荐等级。
- `game_text` 按游戏和语言保存名称、介绍、规则和预写推荐理由。
- `game_player_counts` 按实际人数保存可玩和推荐条件，支持不连续的人数。
- `game_tags` 按游戏和语言保存标签及其展示顺序。

更新资料时，编辑 `sourcedata` 里的 JSON。新增或改名时同步更新 `game_ids.json`，已有游戏保留原 ID。两份原始 JSON 可以独立排序。然后在项目根目录执行 `python -m backend.build_catalog`，导入成功后重启 `python start.py`。导入会校验双语配对、人数、唯一键和外键。校验失败时保留上一次成功生成的数据库。
