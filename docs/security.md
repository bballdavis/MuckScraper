---
title: Security
sidebar_position: 6
---

# Security

By default every page requires a login. Setting `PUBLIC_READ_ACCESS=true` (see [Configuration](./configuration.md#accounts-and-public-access)) opens the reading pages to signed-out visitors. The app is built as a private admin tool either way, so don't expose it directly to the public internet.

Recommended deployment:

- keep the admin interface on a local network, or
- put it behind a VPN such as WireGuard or Tailscale.

The [API](./API.md) is the one part built to sit behind a tunnel. It runs as its own container with only `/api/v1` routes, and it has no admin pages, login or web UI to reach.
