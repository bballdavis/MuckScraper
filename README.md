<p align="center">
  <img src="aggregator/static/favicon_512.png" width="120" alt="MuckScraper logo">
</p>

<h1 align="center">MuckScraper</h1>

<p align="center"><strong>A self-hosted news aggregator with multi-source grouping and local LLM analysis.</strong></p>

<p align="center">
  <a href="https://bballdavis.github.io/MuckScraper/"><img alt="Documentation" src="https://img.shields.io/badge/docs-read%20the%20guide-4c4c8f"></a>
  <a href="https://sample.muckscraper.news"><img alt="Live sample" src="https://img.shields.io/badge/live-sample%20site-1e1e2e"></a>
  <img alt="Docker" src="https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="PostgreSQL with pgvector" src="https://img.shields.io/badge/database-PostgreSQL%20%2B%20pgvector-336791?logo=postgresql&logoColor=white">
  <img alt="Ollama" src="https://img.shields.io/badge/LLM-Ollama%20by%20default-000000">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-your%20hardware-4c4c8f">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue">
</p>

<p align="center">
  <a href="https://bballdavis.github.io/MuckScraper/">Documentation</a> ·
  <a href="https://bballdavis.github.io/MuckScraper/getting-started">Install</a> ·
  <a href="https://sample.muckscraper.news">Live sample</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="screenshots/light_mode.png" alt="MuckScraper headlines in light mode">
</p>

---

MuckScraper pulls news from RSS feeds, NewsAPI and GNews, works out which articles cover the same event, and turns each group into one story. Each story gets a headline, a summary, a count of the left, center and right outlets covering it, and a link to every source. It runs on your own hardware.

## Quick start

You need Docker with the Compose v2 plugin.

```bash
git clone https://github.com/grregis/muckscraper.git
cd muckscraper
./install.sh
```

The first run creates `.env` and stops so you can fill it in. Then run `./install.sh` again and open `http://localhost:5000`.

The [documentation](https://bballdavis.github.io/MuckScraper/) covers the full setup, configuration and hardware guide.

## Features

- **One story, every outlet.** Articles are embedded and grouped, so twelve outlets reporting the same event become one story. Wire copies are counted once.
- **Bias you can see.** Every outlet has a left-to-right rating, from AllSides or from the model. Each story shows how many left, center and right outlets covered it.
- **Summaries of real reporting.** The model summarizes and compares articles that reporters wrote. It never writes the news, and a quality report flags any figure that doesn't appear in the sources.
- **Opinion in its own lane.** Op-eds never count as news coverage. They're linked to the stories they argue about.
- **Editions, not an endless feed.** Twice a day it picks a balanced edition of 20 stories, and every past edition stays readable.
- **Yours.** Runs in Docker with Ollama by default. No subscriptions, no ads.

## Security

By default every page requires a login. Keep the admin interface on a local network or behind a VPN such as WireGuard or Tailscale. Read the [security guide](https://bballdavis.github.io/MuckScraper/security) before you expose anything.

## Built with

Python, Flask, SQLAlchemy, PostgreSQL with pgvector, Ollama, Docker, and Docusaurus for the documentation site.

## License

MuckScraper is released under the [MIT License](LICENSE).
