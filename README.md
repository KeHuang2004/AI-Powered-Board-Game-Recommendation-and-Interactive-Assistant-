# Game Night · Board Game Finder

**English** · [简体中文](README.zh.md)

Game Night is an EE6405 course project that helps a group choose a board game, understand its rules, and start a guided first game. It includes a bilingual catalog of **235 games** and runs locally as a single FastAPI service serving both the web UI and the API. The website opens directly without an application login.

## What you can do

| Feature | What happens |
| --- | --- |
| Find a game | Enter the headcount, host availability, equipment, occasion, mood, and any optional preferences. The service filters by playable headcount, host, and equipment. It ranks eligible games and returns up to three. |
| Refine the results | Add a preference or ask for another batch. The service carries forward the questionnaire answers and excludes games already shown in that recommendation chain. |
| Learn the rules | Read the catalog's core and detailed rules. With a model configured, the page also requests an AI beginner guide based on those rules. |
| Start guided play | Enter player names or use the defaults. Follow a first-game plan, ask rule questions, and use the dice, scoring, and timer controls. |
| Contribute | Open **Contribute** to view the GitHub repository, report a bug, suggest a game, or flag a rule correction. |

## Quick start

Use **Python 3.11–3.14**. Run the commands from the checkout that contains this README. The catalog database is included, so you can start without rebuilding it.

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you already use a Conda environment, activate it and use its `python` in place of `.\.venv\Scripts\python.exe`.

**macOS or Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For a first run without an API key, edit these entries in the root `.env`:

```dotenv
MODEL_PROVIDER=mock
SEARCH_PROVIDER=none
```

Run `python start.py` in an active environment, or `.\.venv\Scripts\python.exe start.py` in PowerShell. The server listens on `127.0.0.1:8001` and opens the English homepage. Switch languages under **Settings**, or open the pages directly:

| Page | Address |
| --- | --- |
| English | [127.0.0.1:8001/en/](http://127.0.0.1:8001/en/) |
| 中文 | [127.0.0.1:8001/zh/](http://127.0.0.1:8001/zh/) |
| Health | [127.0.0.1:8001/health](http://127.0.0.1:8001/health) |
| OpenAPI UI | [127.0.0.1:8001/docs](http://127.0.0.1:8001/docs) |

If port 8001 is busy, run `python start.py --port 8002 --no-browser` and use port 8002 in the URLs. Press **Ctrl+C** to stop the server.

## Model configuration

Recommendation, AI rule explanation, and the Agent Harness all use the model settings in the **root `.env`**. Environment variables already set in the process take precedence. Restart the server after editing the file.

| `MODEL_PROVIDER` | Required settings and behavior |
| --- | --- |
| `mock` | Uses fixed local responses. Useful for rehearsing the full workflow without a model key. |
| `catalog` | Uses catalog ranking and stored rules. Model-based explanations, Q&A, and guided play are unavailable. |
| `deepseek` | Set `MODEL_NAME` and `MODEL_API_KEY`. The provider URL is built in. |
| `gemini` | Set `MODEL_NAME` and `MODEL_API_KEY`. Uses Gemini's OpenAI-compatible endpoint. |
| `compatible` | Set `MODEL_NAME`, `MODEL_API_KEY`, and `MODEL_BASE_URL`, including the required URL prefix such as `/v1`. Use a Chat Completions endpoint with tool calling for guided play. |

For a real provider, use a model enabled for your account and enter the key in your local `.env`. Legacy settings such as `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, and `RECOMMEND_*` are still recognized. Within the same configuration source, an explicitly set `MODEL_*` field takes priority over its legacy equivalent, even when blank. The root `.env` contains the available settings. Optional entries beginning with `##` use application defaults until uncommented.

**Search is configured separately.** `SEARCH_PROVIDER=none` disables new searches. `SEARCH_PROVIDER=mock` uses a sample page to demonstrate search and source cards. `SEARCH_PROVIDER=deepseek` requires access to the configured search service. It uses `SEARCH_API_KEY` first, then attempts `MODEL_API_KEY` or the legacy `DEEPSEEK_API_KEY`.

## How the system works

1. The frontend sends the questionnaire to FastAPI. The default recommender, `s_team`, filters the catalog, orders eligible games by tags and recommended headcount, then asks the model to rerank up to 12 candidates. If the model fails, catalog ranking supplies the results and uses the structured questionnaire fields.
2. Game details read localized rules directly from the catalog. The AI beginner guide is a separate request, so users can read the stored rules while it loads.
3. In the default `t1` mode, the Agent Harness generates or reuses a structured first-game plan. The code advances steps and manages session state. A new chat message can call the configured model and the tools available in that phase. Source badges indicate the stated source category.
4. The app stores sessions and caches in a runtime SQLite database. It records model calls and tool events in local trace files.

| Path | Responsibility |
| --- | --- |
| [frontend/](frontend/) | English and Chinese HTML pages with shared JavaScript and CSS |
| [backend/setting.py](backend/setting.py) | Loads `.env` and serves model and search settings |
| [backend/llm.py](backend/llm.py) | Model clients, provider adapters, and Mock responses |
| [backend/server.py](backend/server.py) | FastAPI entry point, route registration, and guided-play endpoints |
| [backend/database.py](backend/database.py) | Catalog construction and reads, runtime SQLite access, and BM25 rulebook retrieval |
| [recommend_service.py](backend/recommend/recommend_service.py) | Recommendation and refinement endpoints |
| [quick_guide_service.py](backend/quickguide/quick_guide_service.py) | Catalog rules and AI quick-start endpoints |
| [backend/harness/](backend/harness/) and [backend/tutor/](backend/tutor/) | Model and tool orchestration, lesson plans, session state, and rule Q&A |
| [prompts/runtime/](prompts/runtime/) | Prompt templates and tool definitions |

### Prompt layout

```text
prompts/runtime/
  recommend/          Recommendation ranking, request, and output examples
  quickguide/         Rule explanation prompts and guide styles
  agent_harness/
    tutor/            First-game planning, chat, and rulebook context
    probe/            Model capability checks
  shared/             Tool definitions, validation messages, and text tool protocol
```

The loader resolves templates by filename across these folders. Keep filenames unique across the prompt tree.

## Data and SQLite

- [`data/database/boardgames.sqlite3`](data/database/boardgames.sqlite3) is the catalog loaded at startup. Its [schema](data/database/schema.sql) defines shared game records, localized text, playable and recommended headcounts, tags, localized extra fields, and rulebooks.
- [`data/sourcedata/`](data/sourcedata/) holds the English and Chinese JSON, the Excel workbook for extra fields, the stable bilingual ID map, English extra-field translations, and three PDFs for UNO, Blackjack, and Texas Hold’em.
- `data/app.db` is generated at runtime for recommendations, sessions, and caches. `data/traces/` holds model-call traces.

The questionnaire accepts **1–21 people**. The highest supported count in this catalog is **21** for Blood on the Clocktower. Cartographers supports **2–15** in this catalog. Playable headcount is a filter, while recommended headcount helps rank eligible games. The 60 games added in this release have bilingual core and detailed rules. Their English extra fields, such as setup items and win conditions, still need translations.

To update the catalog, keep existing game IDs, append a stable ID and both names for each new game in `game_ids.json`, and update both JSON files and the Excel workbook together. Add English extra fields to `translations.en.json` by game ID. Then run:

```bash
python -m backend.database
```

The builder checks bilingual names, shared metadata, player counts, Excel tags, and database integrity before replacing the catalog. Restart the application to load the rebuilt database.

## API reference

The [OpenAPI UI](http://127.0.0.1:8001/docs) lists the full request schemas. The main routes are:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Return loaded game count, configuration status, and active strategies |
| `POST /recommend` | Rank up to three games and return a recommendation ID (`rid`) |
| `POST /recommend/refine` | Use `rid` and optional `message` to request a new batch |
| `GET /games` and `GET /games/{game_id}/core-rules` | List games and read stored core rules |
| `GET /api/games/{game_id}?lang=en` | Fetch localized game details and rulebook availability |
| `POST /rules/explain` | Generate a guide with `mode: "llm"` or read stored rules with `mode: "raw"` |
| `POST /api/sessions` and `POST /api/sessions/{session_id}/message` | Start guided play or send a message using server-sent events (SSE) |
| `GET /api/sessions/{session_id}` | Restore session state and history |
| `POST /api/sessions/{session_id}/next`, `/jump`, or `/tools/{name}` | Advance a step, jump to a phase, or run a panel tool using the same session URL prefix |

Example `POST /recommend` JSON body:

```json
{
  "players": 4,
  "has_host": false,
  "has_dedicated_components": false,
  "scene": "friends",
  "mood": "relaxed",
  "language": "en",
  "free_text": "We are first-time players."
}
```

## Demo and troubleshooting

1. Open `/health` and check that `games_loaded` is **235**.
2. Try four players, no dedicated host, basic supplies, friends, and relaxed conversation. Check the recommendation source shown on the results page.
3. Open a game, check the stored rules and AI guide, then select **Start guided play** and skip player names.
4. Select **Next step** and send a new question. A new question exercises the model even if the lesson plan came from the cache.

If the page shows catalog ranking or a guide error, check the model provider, name, and key in `.env`, then restart. `/health` reports configuration status. A returned guide or chat response confirms that the request worked. To regenerate a cached plan during a demo, temporarily set `PLAN_CACHE=0` and restart. Mock mode demonstrates the workflow using fixed content. Evaluate real model answers and source quality separately.

## Development and contributions

```bash
python -m pip install -r requirements.txt pytest ruff
python -m pytest -q
ruff check backend tests start.py
```

The automated tests use mock model responses.

For a data change, include the relevant source files and the rebuilt catalog in the same pull request. Report issues or propose changes through the [GitHub repository](https://github.com/KeHuang2004/AI-Powered-Board-Game-Recommendation-and-Interactive-Assistant-).
