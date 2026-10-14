# video-analyzer-local

Video analysis web service against a **local OpenAI-compatible server**. Python 3.11 / FastAPI
backend (`app/`), vanilla JS frontend (`static/`), XML prompt templates (`prompts/`).

## Layout
- `app/analyzers/openai.py` — the only provider. Speaks the OpenAI `/v1` protocol that LiteLLM,
  vLLM, llama.cpp, Ollama and LM Studio all implement. Model list comes from the server.
- `app/analyzers/base.py` — analyzer contract, retry/backoff, exception hierarchy, `close()`.
- `app/video_processor.py` — the only ffmpeg caller: probe, chunk (fixed + silence-aware),
  key frames, clip segments.
- `app/prompt_generation.py` — builds an XML prompt template from a natural-language description.
- `app/structured_outputs.py` — parses keyframe/clip JSON out of model replies.
- `app/prompts.py` — XML prompt loading + `VIDEO_TYPES` registry.
- `prompts/chunk_analysis_knowledge_backup.xml` — lossless mode ("digital twin", Zero-Knowledge
  Loss): exhaustive preservation, not summary; timecodes every 30-60s; artifact registry.
- `docker-compose.yml` — app on :4000; healthcheck `GET /api/config`; `extra_hosts` for host.docker.internal.

## Rules
- A new backend is a config change: set `OPENAI_BASE_URL`. Do not add a provider class unless the
  protocol actually differs. Gemini and OpenRouter were removed on that rule.
- **Never hardcode a model list.** Local model names are not knowable in advance. Report what the
  endpoint returns; on failure return an empty list plus the real error.
- Against a LiteLLM proxy, model discovery uses `/model/info`, which reports per-model
  `supports_vision` and `max_input_tokens`. Plain vLLM/llama.cpp expose only `/v1/models`, so every
  model is offered optimistically.
- Analysis mode = XML file in `prompts/` (+ entry in `VIDEO_TYPES`); never embed prompt text in code.
- Chunk placeholders are substituted by explicit replacement (`PromptManager.format_chunk_prompt`),
  never `str.format` — keyframes/clips specs carry literal JSON braces, and `str.format` aborts the
  whole substitution on the first unknown field.
- ffmpeg is blocking: call it through `asyncio.to_thread`, never inline in an `async def`.
- Analyzers are per-job; close the client and remove temp chunks in `finally`.
- Never write a secret into `prompts/`, `README`, or commits (`.env` is gitignored).
- Upstream sync with `kdoronin/video_analyzer`: `git fetch upstream && git merge upstream/main`.
  Gemini/OpenRouter are gone locally, so an upstream merge deletes nothing and conflicts in
  `app/{config,main,prompts}.py` resolve toward "keep the local single-endpoint version".
  License split per LICENSE/NOTICE: upstream code © its author, our additions Apache-2.0.

## Commands
- `docker compose up -d --build` — the only supported run path.
- `python -m uvicorn app.main:app --reload` — dev, requires `.env`.
- `ruff check --select F,E9 app/` — must be clean before committing.