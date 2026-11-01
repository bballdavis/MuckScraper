---
slug: /
title: Introduction
sidebar_position: 1
---

# MuckScraper

**A self-hosted news aggregator with multi-source grouping and local LLM analysis.**

MuckScraper pulls news from multiple sources, groups related articles into stories, scores outlet bias, and generates local AI summaries and deeper reports on your own hardware.

## Try it first

- **[sample.muckscraper.news](https://sample.muckscraper.news)**: a public, read-only copy of this app, refreshed after every fetch run. It is what you get when you install it, minus the scraped article text (summaries and links to the publishers only).
- **[MuckScraper.news](https://muckscraper.news)**: a newspaper-style site built on the same pipeline. It publishes two balanced editions of 20 stories a day. The newspaper layer is not part of this repository; the aggregator it is built on is.

## What it does

MuckScraper gathers and breaks down the news so you don't have to read the same story twelve times, unless you want to. A few times a day it pulls in articles from RSS feeds (and NewsAPI/GNews if you have keys), works out which ones are about the same event, and turns each pile into a single story. Each story gets one headline, a summary, a look at how the left, center and right are covering it, and a link to every source.

## Why it's different

Most aggregators hand you a list of links. MuckScraper is built around stories, and it does the work on your own machine.

- **One story, every outlet.** Each new article is turned into an embedding (`nomic-embed-text`, stored in pgvector) and compared with recent stories. Close calls get a quick yes/no from the model. Twelve outlets reporting "Senate passes the bill" end up as one story instead of twelve links. Wire copies, where the same AP text runs under three mastheads, are spotted by text overlap and counted once.
- **Bias you can see.** Every outlet carries a left-to-right rating, from AllSides where they rate it and from the model where they don't. Each story shows how many left, center and right outlets covered it, and if one side trails, there's a button to go fetch more from it.
- **Summaries of real reporting.** The model summarizes and compares articles that reporters wrote; it never writes the news. A deep report sets out how the left, center and right are each covering the story, what's contested and what's missing. Guardrails throw out cut-off summaries and model errors posing as headlines, and a quality report flags any figure that doesn't appear in the sources.
- **Opinion in its own lane.** Op-eds never count as news coverage. They're kept separately, linked to the story they argue about, and summed up side by side.
- **Editions, not an endless feed.** Twice a day it picks a balanced edition of 20 stories, holding back repeats unless there's real new coverage. Every past edition stays readable.
- **It's yours, and so is your reading.** It runs in Docker on your own hardware, with Ollama by default. No subscriptions, no ads. Your server fetches every article from every side, so your ISP, or anyone else keeping tabs, only sees you collecting all the news. When you read the scraped copies, which stories you actually read stays on your own machine.

## Where to go next

- [Getting started](./getting-started.md): requirements, installation and the first run.
- [Configuration](./configuration.md): accounts, public access, LLM providers and the knobs you can turn.
- [Features](./features.md): what the app does today.
- [Architecture](./architecture.md): the tech stack and how the code is laid out.
- [Security](./security.md): how to run it safely.
- [API](./API.md), [MCP server](./MCP.md) and [OIDC sign-in](./OIDC.md): optional integrations.
