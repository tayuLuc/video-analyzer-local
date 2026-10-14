# video-analyzer-local

Video analysis web service: chunked multimodal analysis with local and cloud vision models.
Python 3.11 / FastAPI backend (`app/`), vanilla JS frontend (`static/`), XML prompt templates (`prompts/`).

## Layout
- `app/analyzers/{gemini,openrouter,openai}.py` — providers behind `AnalyzerBase`; `openai` speaks any
  OpenAI-compatible `/v1` (Ollama default `:11434`, vLLM via `OPENAI_BASE_URL`).
- `app/prompt_generation.py` — model generates XML prompt templates from a natural-language description.
- `prompts/chunk_analysis_knowledge_backup.xml` — lossless mode ("digital twin", Zero-Knowledge Loss):
  exhaustive preservation, not summary; timecodes every 30-60s; artifact registry (versions/flags/names).
- `docker-compose.yml` — app on :4000; healthcheck `GET /api/config`; `extra_hosts` for host.docker.internal.

## Rules
- Provider addition = subclass of `AnalyzerBase` in `app/analyzers/` + registration; no core changes.
- Analysis mode = XML file in `prompts/` (+ entry in `VIDEO_TYPES`); never embed prompt text in code.
- Never write a secret into `prompts/`, `README`, or commits (`.env` is gitignored). API keys live in env/UI.
- Upstream sync (parent `kdoronin/video_analyzer`): `git fetch upstream && git merge upstream/main` —
  known conflict surface `app/{config,main,prompts}.py`, `docker-compose.yml`, `.env.example`, UI trio;
  new files never conflict. License split per LICENSE/NOTICE: upstream code © its author, our additions Apache-2.0.

## Commands
- `docker compose up -d --build` — the only supported run path.
- `python -m uvicorn app.main:app --reload` — dev, requires `.env` or UI keys.
