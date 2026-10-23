Hi again! This one is a bit of a "hear me out" issue. I know the README calls Meilisearch a great fit, and honestly it's a nice piece of software. I only started digging into this recently, but my main goal is keeping my server's overhead down wherever I can, and Postgres can already do the searching we need. I've got it running that way on my fork, so here's the case, plus some numbers.

## 1. Less overhead, and it's more than we need at this size

MuckScraper keeps roughly 120,000 articles per six months on my install. That's small for Postgres, and Postgres already has everything we need built in: `tsvector` columns, GIN indexes, ranking, and `pg_trgm` for typo tolerance.

It also turns out every search already ends in Postgres anyway. Right now `search_story_ids()` and `search_article_ids()` ask Meilisearch for a list of IDs, and then the page loads those rows from Postgres. So Meili is a second copy of the data that we have to keep in sync:

- `index_changed_since()` runs after every pipeline run to push new and changed stories and articles over.
- `reindex_all()` rebuilds the whole thing, and there's a note in `search.py` that a full reindex on 2026-09-30 got the gunicorn worker killed 70 seconds in and left the index two months stale.
- It's one more container running all the time, with its own memory and disk (`meili_data`), one more secret (`MEILI_MASTER_KEY`), one more status light in the sidebar and one more thing for `install.sh` to set up. Dropping it means less RAM, less disk and less to babysit on the box.

## 2. Data sharing

Meilisearch collects telemetry by default. From their own docs:

> "Meilisearch automatically collects data from all instances that do not opt out using this flag."
> https://www.meilisearch.com/docs/learn/configuration/instance_options (see "Disable analytics")

Their telemetry page says the data goes through Segment and into Amplitude, and lists what's in it: things like system info (OS, kernel, CPU cores, RAM, disk size, server provider), database size and number of indexes, request counts and response times, user agents, and which search features you use (filters, sort, typo tolerance and so on). They say they never collect IP addresses, emails or URLs, which is good.
https://www.meilisearch.com/docs/learn/resources/telemetry

To be fair, our `docker-compose.yml` already sets `MEILI_NO_ANALYTICS: "true"`, so a stock install is opted out today. My worry is that it's opt-out, not opt-in. Anyone who runs Meili their own way, copies the service into another compose file, or upgrades and misses the setting is quietly phoning home. For a project whose pitch includes "it's yours, and so is your reading," I'd rather not have a component in the stack where privacy depends on one env var staying put. Dropping it makes that a non-issue.

## What I did on my fork

It's in one PR on my fork if you want to look: https://github.com/bballdavis/MuckScraper/pull/7

- Stories and articles each get a generated `search_vector` column with a GIN index. Postgres keeps it up to date on every write, so there's nothing to sync and nothing to rebuild, ever.
- Weights: titles and headlines A, summaries B, source and deep report C, article text D.
- Matching uses English word stems, and the last word of a plain query is also matched as a prefix ("ukra" finds Ukraine), so as-you-type search still feels instant.
- Quoted phrases, `OR` and `-word` work like web search.
- When word matching finds only a few hits, titles are also matched by trigram similarity (`pg_trgm`), which catches typos and partial names.
- I kept the function names in `aggregator/search.py` the same so the rest of the code barely changed.
- One migration adds the columns and indexes. The `meilisearch` service, the scheduler sync step, the reindex tools, the `/meili-status` route and the sidebar light all go away.

The diff came out to 25 files, +378 / -811, so it's a net simplification. Search has been quick and accurate in my testing so far, and the live search dropdown runs on it:

![Live search dropdown on Postgres search](https://raw.githubusercontent.com/bballdavis/MuckScraper/issue-screenshots/redesign/search-suggest.png)
