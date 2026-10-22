# One image for the app, scheduler and MCP server; each service in
# docker-compose.yml picks its own command. No browser or X11 libraries:
# nothing in the app drives a headless browser.
FROM python:3.10-slim

# git: the MCP server's code_live_check reads git metadata from the
# bind-mounted checkout. psycopg2-binary and the other wheels need nothing else.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY mcp_server/requirements.txt ./mcp-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r mcp-requirements.txt

COPY aggregator ./aggregator
COPY news_fetcher ./news_fetcher
COPY mcp_server ./mcp_server
COPY boot.sh ./boot.sh
# Schema setup and the first admin account, so a deploy without the
# bind-mounted checkout (e.g. Komodo) can still run migrations.
COPY bootstrap_admin.py ./bootstrap_admin.py
COPY migrations ./migrations

RUN git config --global --add safe.directory /app

EXPOSE 5000

# The web app; the scheduler and mcp services override this in compose.
CMD ["./boot.sh"]
