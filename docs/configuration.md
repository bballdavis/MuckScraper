---
title: Configuration
sidebar_position: 3
---

# Configuration

## Accounts and public access

Add accounts at **Users** in the account menu (top right). Each has one of three roles:

- **Reader**: reads everything, including full article text and search, and changes nothing.
- **Scraper**: also fetches articles and runs summaries, analysis, bias ratings and scrapes.
- **Admin**: also uses Admin Tools, changes all configuration, and manages users.

Everyone can change their own email and password on their **Profile**.

Set `PUBLIC_READ_ACCESS=true` in `.env` to let signed-out visitors read Headlines, All Stories, Grouped Stories (with the topic filters), story pages and article summaries. Search and Fetch still need an account. Signed-out visitors never see scraped full text, scrape details or any button that runs work. It is off by default; after changing it, run `docker compose up -d app`.

Dates are shown in the time zone set by `DISPLAY_TIMEZONE` in `.env` (an IANA name such as `America/New_York`; default `UTC`). After changing it, run `docker compose up -d app`; a plain restart doesn't re-read `.env`.

## LLM behavior

**Choosing a provider (`LLM_PROVIDER`).** Ollama is the default and the point of the project, but `LLM_PROVIDER` also accepts `lmstudio`, `gemini`, `anthropic`, `groq`, and `openrouter`.

The OpenRouter path is a generic OpenAI-compatible client. Set `OPENROUTER_HOST` to any endpoint speaking `chat/completions` (DeepSeek, OpenAI, Together, a local vLLM) and it works with no code change. Embeddings are a separate axis (`EMBEDDING_PROVIDER`), and only Ollama, LM Studio and Gemini can serve them. Anthropic and Groq have no embedding models, and OpenRouter routes chat completions only.

`lmstudio` talks to LM Studio's local server (`LMSTUDIO_HOST`, default `http://localhost:1234/v1`), so a desktop running LM Studio can serve the embeddings (`LMSTUDIO_EMBEDDING_MODEL`, a 768-dimension model such as `text-embedding-nomic-embed-text-v1.5`) while Anthropic writes the text. All of these can be saved in Settings › LLM providers instead.

`anthropic` calls the Messages API directly with `ANTHROPIC_API_KEY`. Set `ANTHROPIC_MODEL` (summaries) and optionally `ANTHROPIC_FAST_MODEL` (the mechanical calls), for example a Sonnet model and a Haiku model.

**Splitting the two workloads (`LLM_FAST_PROVIDER`).** A run makes about 1,140 mechanical calls against about 65 summary and deep-report calls. Those two groups can go to *different* providers, so a GPU that can't run a model good enough for summaries can still handle the volume:

```
LLM_PROVIDER=openrouter      # the ~65 summaries and deep reports
LLM_FAST_PROVIDER=ollama     # the ~1,140 grouping/classification/bias calls
```

The volume stays local and free; only the handful of calls a reader actually sees go to the cloud. `LLM_PROVIDER` is the summary provider, and `LLM_FAST_PROVIDER` overrides it for the mechanical calls. Leave `LLM_FAST_PROVIDER` blank to send everything to one provider.

Health checks are per tier, so the pipeline degrades rather than stops: if the local box is asleep, summaries still run and only classification is skipped, and the other way round.

**Model tiers (`OLLAMA_FAST_MODEL`).** A full pipeline run makes roughly 1,200 sequential LLM calls, and about 1,140 of them are mechanical: story-grouping confirmations, topic classification, headline generation, outlet bias. Their output is a label, a yes/no, or a short headline. Only about 65 are the summaries and deep reports a reader actually sees. Setting `OLLAMA_FAST_MODEL` to a smaller model routes the mechanical calls to it while summaries stay on `OLLAMA_MODEL`.

This matters most when `OLLAMA_MODEL` is too large for your GPU. Compare `size_vram` against `size` in `curl $OLLAMA_HOST/api/ps`. If `size_vram` is smaller, the rest is running on the CPU and every call pays for it. Worse, a model that fills the card leaves no room for the embedding model, so Ollama swaps the two in and out on every article. A fast model that fits alongside `nomic-embed-text` avoids both problems.

Leave it blank to use `OLLAMA_MODEL` for everything. `GEMINI_FAST_MODEL` and `GROQ_FAST_MODEL` do the same for those providers.

Prompt wording itself is editable at `/admin/prompts` with no code change needed. The surrounding logic (persona and analysis-type selection, story-grouping thresholds and so on) lives in:

- `news_fetcher/summarizer.py`
- `news_fetcher/topic_classifier.py`
- `news_fetcher/story_grouper.py`
- `news_fetcher/headline_generator.py`
- `news_fetcher/outlet_bias_llm.py`
- `news_fetcher/allsides_lookup.py` (bias data source)

## Customization

Topics, feeds, queries, prompts, filters and the schedule are all stored in the database and editable in the admin pages. No code change is needed:

- Topics: `/admin/topics`
- RSS feeds: `/admin/rss-feeds`
- LLM prompts: `/admin/prompts` (each resettable to its original default)
- Pipeline run schedule, meaning *when* fetch-only and full-pipeline runs happen: `/admin/pipeline-schedule`
- Scheduled fetches, meaning *what* each run pulls from NewsAPI and GNews: `/admin/scheduled-fetches`
- Ingestion blocklist, meaning the sources and headline keywords refused before anything is stored: `/admin/ingestion-blocks`

The shipped defaults (US-centric topics, a gaming-news filter) are only a starting point. Change them on these pages to suit your own beat.

## Scrape and grouping tuning

The important knobs live in code:

- similarity thresholds in `news_fetcher/story_grouper.py`
- heuristic ingest filters (roundup, advice-column and betting title patterns) in `aggregator/article_signals.py`. The source and headline-keyword blocklists themselves are admin-editable at `/admin/ingestion-blocks`.
- retry and cooldown behavior in `news_fetcher/scraper.py`

## Paywalls

Paywalled sites are not bypassed: they stay headline-and-description only. With an optional free `NYT_API_KEY` (set on Settings › Source API keys or in `.env`), MuckScraper reads NYT's Times Wire through the day and ingests its news sections with the abstract, plus the opening paragraph from Article Search for a limited number of articles per request, all within the key's daily request limit (see `news_fetcher/nyt_api.py`). A New York Times article gets that text instead.
