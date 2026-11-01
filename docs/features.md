---
title: Features
sidebar_position: 4
---

# Features

## Screenshots

### Headlines
The latest published edition, with each story's Left / Center / Right coverage count and summary.

![Headlines in light mode](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/light_mode.png)

### Grouped Stories (dark mode)

![Grouped Stories in dark mode](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/dark_mode.png)

### Multi-source story view
One story, 88 articles from 22 outlets: the summary up top, then every source with its outlet's leaning.

![Story Reader](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/story_reader1.png)
![Story sources](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/story_reader2.png)

### Opinion
Columns are kept out of the news but linked to the story they argue about, filed by the outlet's leaning, each with a one-line stance and a short roundup of what each side is saying.

![Opinion on a story](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/story_opinion.png)
![Op/Ed page](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/oped.png)

### Bias tags
Every card counts the left, center and right outlets covering the story. When one side trails, a button fetches more coverage from that side.

![Bias Tags](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/bias_tags1.png)
![Bias Tags](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/bias_tags2.png)

### Search
Stories and articles in dense, sortable tables, filtered by time window, with Left / Center / Right counts per story.

![Search](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/search.png)

### Article reader

![Article Reader](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/article_reader1.png)
![Article Reader](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/article_reader2.png)

### Past editions

![Past editions](https://raw.githubusercontent.com/bballdavis/MuckScraper/dev/screenshots/editions.png)

## Editions and ranking

- Configurable scheduled editions per day
- Past editions page, grouped by month, each edition readable as it was published plus anything added to its stories since
- Opinion page per edition: the stories that got new opinion columns, with each column's stance
- 20-story edition target
- Repeats held back unless there is meaningful new coverage
- Carry-over logic for underfilled editions
- Publish-time duplicate-story filtering for same-event headline candidates

## News fetching

- Scheduled multi-topic fetches
- On-demand fetch by topic or custom query
- NewsAPI and GNews support
- RSS ingestion, with an optional fallback URL per feed for when a feed goes down
- Outlets without a working feed can come in through Google News search feeds. Links are resolved to the publisher's own URL before anything is stored.
- Default feeds include free nonprofit newsrooms (The Texas Tribune, ProPublica, CalMatters, KFF Health News, Inside Climate News), so those stories arrive with their full text
- Duplicate article detection by URL and normalized title and outlet checks
- Copied articles (wire copies, one outlet running another's piece) are marked and not counted as extra sources
- Junk is filtered before storage: betting picks, fantasy advice, roundups, editorial cartoons, plus your own source and title blocks
- Full-text search across articles and stories in Postgres, always current (no index to rebuild), with prefix and typo matching for as-you-type search

## Scraping and reliability

- Full article scraping during ingestion
- Multi-step scrape fallback pipeline
- Scrape telemetry stored per article
- Bad-scrape auditing and status-aware retries
- Domain and URL cooldown behavior for repeated failures
- Admin monitoring for scrape outcomes and blocklist behavior

## Story grouping and summaries

- Vector-based story grouping with pgvector
- LLM-assisted handling of borderline matches
- AI story headlines for grouped stories
- Story summaries and deeper reports
- Per-article summaries and deep analysis for edition articles
- Op/Ed kept apart from the news: its own page, opinion RSS feeds, columns linked to the stories they argue about, and a short opinion roundup per story
- Stable-story skipping, so unchanged stories are not reprocessed

## Reading and search

- Headlines, Grouped Stories (two or more articles) and All Stories views, with topic filters
- Time windows (24 hours, 7 days, 30 days, all time) on the story lists and search
- Search page with sortable Stories and Articles tables and custom date ranges
- Back to Feed returns to the card or search row you left
- Relative timestamps ("3 hours ago") with the exact local time on hover
- Light and dark themes
- Reader, Scraper and Admin accounts, and optional public read-only access

## Bias and metadata

- Outlet bias labels, sourced from AllSides or from a model
- Topic classification
- Image capture from upstream feeds
- Archived edition-story image support for stable published output

## Admin tools

- Manual scrape and rescrape actions
- Bulk scrape-missing workflow
- Scrape audits
- Ollama helpers: wake the Ollama box, catch up after an outage (missing embeddings, regrouping of single-article stories, headlines), and reclassify topics
- Outlet merge tooling
- Topic, RSS feed, scheduled fetch and ingestion blocklist management
- Editable LLM prompts, each resettable to its original default
- Pipeline run schedule management (add, edit and delete when fetch-only and full-pipeline runs happen)
- Container restart from the admin UI, blocked automatically while a fetch or other background task is running
