---
title: Getting started
sidebar_position: 2
---

# Getting started

## Requirements

- Docker with the Compose v2 plugin (`docker compose`). PostgreSQL with pgvector runs as part of the stack.
- An LLM provider: Ollama by default, or Gemini, Groq, or any OpenAI-compatible endpoint
- An embedding provider: Ollama (`nomic-embed-text`) or Gemini
- Optional: NewsAPI and GNews API keys. Each source is skipped if its key is blank; the default RSS feeds work without either.

## Hardware

What the live deployment runs on, as a guide:

- **The Docker stack:** 2 CPU cores and 8 GB of RAM is plenty (the containers use about 1.5 GB between them). Allow about 10 GB of disk for the images, plus the database: about 1 GB for 120,000 articles over six months, including its search indexes.
- **The LLM:** a GPU makes the difference. A full run makes about 1,200 LLM calls, one after another, so every second per call adds up. The live site uses a 10 GB GPU with `gemma4:12b-it-qat` (`OLLAMA_THINK=false`) and `nomic-embed-text`, both held on the GPU together; a full run of 250-300 new articles takes 25-35 minutes. On a smaller card, `gemma4:e4b-it-qat` (about 3 GB of VRAM) works. Ollama can run on a different machine from the stack (set `OLLAMA_HOST`).
- **Ollama's context size:** set `OLLAMA_CONTEXT_LENGTH` to at least `16384` on the Ollama server (in its service environment, not in this `.env`). Older Ollama versions default to 4,096 tokens, which cuts off long story-summary prompts. MuckScraper never asks for a context size per call, so the server's setting is what every call gets.
- **No GPU?** Send the summaries, or everything, to a cloud provider instead; see [LLM behavior](./configuration.md#llm-behavior).

## Installation

```bash
git clone https://github.com/grregis/muckscraper.git
cd muckscraper
./install.sh
```

The first run creates `.env` from `.env.sample`, fills in `SECRET_KEY` for you, and stops so you can fill in the rest. At minimum set `OLLAMA_HOST`, `OLLAMA_MODEL`, `EMBEDDING_MODEL` (`nomic-embed-text`) and `ADMIN_PASSWORD`; pull both models on your Ollama box first (`ollama pull ...`). Change the Postgres password too. News API keys are optional.

Run `./install.sh` again once that's done. It builds the images (a few minutes the first time), sets up the database (pgvector extension and tables), creates the admin user, and starts the scheduler. Safe to re-run: on an existing install it applies any pending migrations rather than skipping them. A very old install with tables but no migration history stops with instructions instead of guessing its schema version.

Then open `http://localhost:5000` and sign in with `ADMIN_USERNAME` and `ADMIN_PASSWORD` from `.env`. The app only listens on localhost; to reach it from another machine, set `APP_BIND=0.0.0.0` (or a LAN address) in `.env` and run `docker compose up -d app`, after reading [Security](./security.md). `APP_PORT` moves it off port 5000.

**What to expect on day one.** The scheduler starts a first run as soon as it comes up. On an empty database it has hundreds of new articles to group and summarize, so it takes a good while longer than a normal run; watch it with `docker compose logs -f scheduler`. Headlines stays empty until the first full run publishes an edition. After that, runs follow the schedule: full runs at 7:00 and 17:00 and fetch-only runs at 12:00 and 22:00, US Eastern time. Change them at `/admin/pipeline-schedule` (then restart the scheduler).

### Manual steps

If you'd rather not use `install.sh`:

```bash
cp .env.sample .env && chmod 600 .env
# Edit .env: Ollama host and models, admin password, and SECRET_KEY from:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d --build postgres app
docker compose exec app python bootstrap_admin.py
docker compose up -d scheduler
```

## Upgrading

```bash
git pull
./install.sh
```

`install.sh` rebuilds the images and applies any database migrations. Read the release's `CHANGELOG.md` entry first. To run only the migrations: `docker compose exec app flask db upgrade`.

Upgrading within a major version (for example `1.x` to `1.y`) never breaks your stored config or customizations: topics, RSS feeds, prompts, scheduled fetches, and ingestion blocks you've edited stay as you left them. Config-breaking changes are reserved for major version bumps (`1.x` to `2.0`) and always called out explicitly in that release's `CHANGELOG.md` entry, never shipped silently in a minor or patch upgrade.

## Optional integrations

MuckScraper can be extended with personal workflow hooks, such as n8n webhooks for fetch reports or Ollama power management, and Matrix notifications for status messages. These are not part of the default Docker Compose setup; add them with your own environment variables, compose override, or notification code if you want those workflows.

- **MCP server:** a read-only server for AI assistants. See [MCP](./MCP.md).
- **API:** a JSON API for phones and scripts. See [API](./API.md).
- **OIDC sign-in:** sign in with an identity provider. See [OIDC](./OIDC.md).
