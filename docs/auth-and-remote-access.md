# Auth And Remote Access

codex-queue is built for personal use on a trusted host. The dashboard is safe
for loopback and private network access, not public unauthenticated exposure.

## Password Gate

Set:

```bash
CODEX_QUEUE_PASSWORD='your-password-here' codex-queue-web
```

Sessions use an HttpOnly, SameSite=Lax, 7-day cookie named `cq_session`.
`Secure` is enabled by default; set `CODEX_QUEUE_COOKIE_SECURE=0` for plain
local HTTP development.

## Recommended Remote Access

Use Tailscale, Cloudflare Access, or SSH port forwarding in front of the
dashboard. If a stronger network-level gate is already present, leaving
`CODEX_QUEUE_PASSWORD` unset keeps the dashboard auth layer disabled.

## Cloudflare Tunnel Sketch

```yaml
tunnel: <id>
credentials-file: /path/to/<id>.json
ingress:
  - hostname: codex-queue.example.com
    service: http://localhost:51002
  - service: http_status:404
```

Point DNS at the tunnel and put Cloudflare Access in front of it for public
internet exposure.
