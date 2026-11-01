---
title: Architecture
sidebar_position: 5
---

# Architecture

## Tech stack

- **Backend:** Python, Flask, SQLAlchemy
- **Database:** PostgreSQL with pgvector
- **Search:** Postgres full-text search (`tsvector` and `pg_trgm`)
- **News data:** NewsAPI and GNews, with RSS support
- **LLM runtime:** Ollama by default; Gemini, Groq, or any OpenAI-compatible endpoint optional
- **Embeddings:** `nomic-embed-text`
- **Scraping:** BeautifulSoup, readability-lxml
- **Runtime:** Docker and Docker Compose

## Project structure

```text
muckscraper/
├── aggregator/
│   ├── __init__.py                 # App factory
│   ├── app.py                      # Main Flask entry point
│   ├── models.py                   # SQLAlchemy models
│   ├── filters.py                  # Jinja filters and display helpers
│   ├── constants.py                # Shared constants (topics are DB-backed)
│   ├── display_time.py             # DISPLAY_TIMEZONE handling for shown dates
│   ├── html_safety.py              # Sanitising filters for scraped and LLM text
│   ├── seed_defaults.py            # Default topics, feeds, prompts and schedule for a fresh install
│   ├── article_signals.py          # Ingest heuristics: roundup and betting patterns, bias bucketing, independent-source checks
│   ├── search.py                   # Full-text search (Postgres)
│   ├── story_view.py               # Story view helpers
│   ├── blueprints/
│   │   ├── admin/                  # Admin and maintenance routes
│   │   ├── auth.py                 # Login and logout routes
│   │   └── public.py               # Public reader routes
│   ├── static/                     # Shared static assets (css/theme.css holds the colour tokens)
│   └── templates/                  # Jinja templates
├── docker_restart_proxy/           # Small service that lets the admin UI restart containers (and the MCP server read logs)
├── mcp_server/                     # Optional read-only MCP server for AI assistants
├── docs/                           # The documentation, also built into the docs site
├── website/                        # The Docusaurus config for the docs site
├── migrations/                     # Alembic migration files
├── news_fetcher/
│   ├── fetch_and_store_articles/   # Ingestion, grouping, and edition publishing (package)
│   ├── llm_client.py               # LLM provider dispatch (Ollama, Gemini, Groq, OpenAI-compatible)
│   ├── prompt_registry.py          # DB-backed prompt templates
│   ├── quality_checks.py           # Output-quality detectors (pure functions)
│   ├── quality_report.py           # Read-only quality report for a run
│   ├── rss_fetcher.py              # RSS ingestion helpers
│   ├── google_news.py              # Resolves Google News search-feed links to publisher URLs
│   ├── copy_detector.py            # Marks copied articles (wire copies) within a story
│   ├── opinion_linker.py           # Links Op/Ed columns to stories, writes opinion roundups
│   ├── scheduler.py                # Scheduled fetch runner
│   ├── scraper.py                  # Scrape pipeline and fallback logic
│   ├── story_grouper.py            # Story clustering logic
│   ├── summarizer.py               # Story and article summaries
│   ├── topic_classifier.py         # Topic classification helpers
│   ├── headline_generator.py       # AI headline generation for grouped stories
│   ├── allsides_lookup.py          # AllSides bias data lookup
│   ├── outlet_bias_llm.py          # LLM-based outlet bias scoring
│   ├── backfill_images.py          # Utility: backfill missing story images
│   └── cleanup_duplicates.py       # Utility: deduplicate articles and stories
├── tests/                          # Automated tests
├── boot.sh                         # Starts gunicorn for the web app
├── install.sh                      # First-time setup and upgrades
├── bootstrap_admin.py              # Admin user creation script
├── docker-compose.yml              # Local stack definition
├── Dockerfile                      # The one image for app, scheduler and MCP server
├── requirements.txt                # Python dependencies
└── .env.sample                     # Example environment configuration
```

## Tests

Run the test suite with the Python standard library's runner:

```bash
python3 -m unittest discover -s tests
```

The tests need `SECRET_KEY` set, and the Postgres-backed ones need a database URL in `API_TEST_DATABASE_URL`.

## Release notes

Release history and upgrade notes are in the [CHANGELOG](https://github.com/bballdavis/MuckScraper/blob/dev/CHANGELOG.md).

This repository documents the main application and ingestion pipeline. It does not document every deployment-specific integration. Optional local integrations can exist around the core stack without being required for the open-source app itself.

## Special thanks

- **[Langfuse](https://langfuse.com/)**: LLM observability and tracing, invaluable for debugging prompts and iterating on model behavior during development.
- **[AllSides](https://www.allsides.com/)**: outlet bias ratings that inform MuckScraper's bias labeling. Their commitment to balanced news exposure is very much in the spirit of this project.
