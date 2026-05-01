# Auth and remote access

The web server binds to `127.0.0.1:51002` by default — not reachable from outside the host. Pick one of three options below to access it remotely. **Tailscale is recommended for personal use.**

## Option A — Tailscale (recommended)

Zero public exposure, machine-level auth via WireGuard.

**Prereqs**

- Install Tailscale on the host and every device you want to use.
- In the Tailscale admin console: enable **MagicDNS** (Settings → DNS) and **HTTPS Certificates** (Settings → HTTPS Certificates). HTTPS is needed so `tailscale serve` can issue a real Let's Encrypt cert for your MagicDNS name.

**Expose**

```bash
tailscale serve --bg http://127.0.0.1:51002
```

`tailscaled` now proxies HTTPS port 443 on the host's tailnet IP to your local `127.0.0.1:51002`. Any device in your tailnet reaches it at `https://<host-name>.<tailnet-name>.ts.net/` (check `tailscale status` for the exact hostname). No password needed — only devices signed into your tailnet can route to it.

**Stop sharing**

```bash
tailscale serve --https=443 off
```

The share persists across reboots once set, so you only run `tailscale serve` once. Because the server binds to loopback, leaving `tailscale serve` off means no remote access at all.

## Option B — Cloudflare Tunnel (+ optional Cloudflare Access)

Public URL, best if you want to reach the dashboard from a device that isn't on your tailnet. Requires a domain on Cloudflare.

1. Set up a tunnel with `cloudflared` and add an ingress rule:
   ```yaml
   ingress:
     - hostname: queue-worker.example.com
       service: http://localhost:51002
   ```
2. Point a DNS CNAME at the tunnel: `cloudflared tunnel route dns <id> queue-worker.example.com`
3. **Strongly recommended**: put a [Cloudflare Access](https://www.cloudflare.com/products/zero-trust/access/) application in front of it and restrict by email / Google SSO. Free tier covers 50 users. Cloudflare authenticates before requests hit your server.
4. If you don't use Access, enable the password layer instead.

## Option C — Password auth only (no tunnel, no VPN)

If you're exposing the server directly (e.g., binding to `0.0.0.0` on a trusted LAN), the built-in password gate is the minimum you should run.

## Authentication

Optional — only needed when the dashboard is reachable from somewhere untrusted. Tailscale (Option A) and Cloudflare Access (Option B with Access) both remove the need for a dashboard password. Leave `QUEUE_WORKER_PASSWORD` unset and the auth layer is a no-op: no login page, every route open.

**Enable**

```bash
QUEUE_WORKER_PASSWORD='your-password-here' queue-worker-web
```

Keep the env var out of version control — set it in your shell profile, in the tmux command that launches the server, or in a gitignored `~/.env`-style file. Any UTF-8 string works (including non-ASCII); it's compared as raw bytes.

### Security model

Sessions use an HttpOnly, SameSite=Lax, 7-day cookie (`qw_session`, `Secure` unless `QUEUE_WORKER_COOKIE_SECURE=0`). Passwords are compared constant-time with `hmac.compare_digest` on UTF-8 bytes.

Brute-force protection is two-layered:

- **Per-IP**: 5 wrong attempts → 10-min lockout (during which even the right password is rejected).
- **Global**: 50 failures across all IPs in any 60-second window → 10-min lockout for everyone, so rotating-IP attacks can't bypass the per-IP limit.

Concurrent attempts from the same IP serialize under a single lock. When behind a Cloudflare tunnel the real client IP is read from `CF-Connecting-IP`, but only when the request peer is localhost (prevents header spoofing from other network paths).

**What this doesn't protect against**: a compromised client machine, a leaked password, or an attacker already past your tunnel/VPN. For public deployments, use Tailscale or Cloudflare Access instead of (or in addition to) this.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/login` | Login page (HTML) |
| `POST` | `/login` | `{"password": "..."}` → 200 + `Set-Cookie` / 401 / 429 |
| `POST` | `/logout` | Clear server session + cookie |

### Scripting against the API with auth enabled

```bash
# Log in, save cookie
curl -c cookies.txt -X POST http://localhost:51002/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"your-password"}'

# Use the cookie for subsequent calls
curl -b cookies.txt http://localhost:51002/api/status
```

### Response shapes

| Situation | Status | Body |
|---|---|---|
| Wrong password (retry OK) | `401` | `{"detail":"wrong_password","remaining_attempts":4}` |
| Locked out | `429` | `{"detail":"ip_locked","retry_after":598}` or `"global_locked"` |
| Unauthenticated API call | `401` | `{"detail":"not authenticated"}` |
| Unauthenticated HTML request | `302` | `Location: /login` |
