# Caddy in front of the AI DJ (CT116)

Caddy is the only thing listening on the public side of CT116 (Ubuntu LXC,
`/opt/null-set-ai-dj`): `:80` for the ACME challenge and the HTTP -> HTTPS redirect,
`:443` for everything else. The console and the stem server bind `127.0.0.1` and are
reached only through Caddy. Config: [`deploy/Caddyfile`](Caddyfile). Sign-in itself is the
app's job: see [`docs/engineering/AUTH.md`](../docs/engineering/AUTH.md).

```
internet -> router 80/443 -> Proxmox host 192.168.1.2 (vmbr0 DNAT) -> CT116 10.0.0.116:80/443 (Caddy)
            Caddy  /stems/*  -> 127.0.0.1:$STEM_PORT   (app/stem_server, prefix stripped)
            Caddy  /*        -> 127.0.0.1:$DJ_PORT     (uvicorn app.ui.server)
```

Validation: Caddy was not installed where this file was written, so `caddy validate` has
not been run on it yet. Run it (below) before the first start.

## What the Caddyfile does

* Site address from `{$DJ_DOMAIN}`; automatic HTTPS with Let's Encrypt (HTTP-01 / TLS-ALPN on
  80 / 443). No domain is stored in the repo.
* `/stems/*` -> stem server with `handle_path` (prefix stripped); everything else -> console.
* `reverse_proxy` with `flush_interval -1` (streams flush at once; websockets need nothing
  extra). Caddy sets `X-Forwarded-For` / `-Proto` / `-Host` itself and ignores ones sent by
  the client; it keeps the original `Host`, which the app's CSRF check compares with `Origin`.
* `request_body max_size` per route, 1 MB above the app's own caps (tracks 300 MB, samples
  26 MB, recordings 1 GB, other requests 64 MB, stems 200 MB). Timeouts: 15 min to read a
  body, no write cap (audio streams), 15 min to the upstream.
* Headers: HSTS 1 year (no `preload`, no `includeSubDomains`), `nosniff`,
  `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` plus CSP `frame-ancestors 'none'`
  (only where the app sent no CSP of its own), `Server` removed.
* JSON access log at `/var/log/caddy/dj-access.log`, rolled at 50 MiB, 10 files, 30 days;
  the `code` and `state` query parameters (Google's callback) are replaced with `REDACTED`.
  Caddy redacts `Authorization`, `Cookie` and `Set-Cookie` in logs by default. Never add
  `log_credentials`.
* A commented IP allow-list (`remote_ip`) if the site should only answer certain networks.

**CSP: TODO.** A full Content-Security-Policy is not set. The console uses inline event
handlers (`onclick="..."`) and loads WaveSurfer from unpkg and fonts from Google, so a
strict policy breaks it, and no policy has been tested against it. Before adding one, move
the inline handlers into the JS files and pin or self-host the CDN scripts, then check the
console loads with the policy in report-only mode first.

## The app side (must match)

In `/opt/null-set-ai-dj/.env`:

```
DJ_TRUSTED_PROXIES=127.0.0.1,::1      # the default; Caddy is local
# DJ_ALLOWED_ORIGINS=                 # empty: same-origin only (the console is served by the app itself)
```

Run uvicorn on `127.0.0.1` with `--no-proxy-headers` (start.sh does both), so the app's own
`DJ_TRUSTED_PROXIES` is the only thing that decides whether `X-Forwarded-*` is believed.
The stem server's env file: `STEM_SERVER_HOST=127.0.0.1`.

## Install (owner, on CT116)

```bash
# official Caddy apt repository (https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# the env file (DJ_DOMAIN is required; ports only if not the defaults)
printf 'DJ_DOMAIN=dj.example.org\nDJ_PORT=8000\nSTEM_PORT=47830\n' | sudo tee /etc/caddy/dj.env
sudo chmod 640 /etc/caddy/dj.env && sudo chown root:caddy /etc/caddy/dj.env

# point the packaged unit at the repo's Caddyfile and the env file
sudo systemctl edit caddy        # add:
#   [Service]
#   EnvironmentFile=/etc/caddy/dj.env
#   ExecStart=
#   ExecStart=/usr/bin/caddy run --environ --config /opt/null-set-ai-dj/deploy/Caddyfile --adapter caddyfile
#   ExecReload=
#   ExecReload=/usr/bin/caddy reload --config /opt/null-set-ai-dj/deploy/Caddyfile --adapter caddyfile --force

sudo install -d -o caddy -g caddy /var/log/caddy
set -a; . /etc/caddy/dj.env; set +a
caddy validate --config /opt/null-set-ai-dj/deploy/Caddyfile --adapter caddyfile
sudo systemctl restart caddy && journalctl -u caddy -f
```

`dj.example.org` above is a placeholder; use the real name only in `/etc/caddy/dj.env`.

**DNS challenge instead** (ports 80/443 can't be forwarded, or the name should get a cert
before it is public): build Caddy with the DNS provider module (`xcaddy build --with
github.com/caddy-dns/<provider>`, or `caddy add-package`), then in the site block:

```
tls {
	dns <provider> {env.DNS_API_TOKEN}
}
```

with the provider token in `/etc/caddy/dj.env`.

**Public port other than 443** (the router's 443 belongs to another service, e.g. router
8448 -> CT116 443): Caddy still listens on 443 inside CT116 and matches the site by host name
(the `:8448` in the client's `Host` header is ignored for matching and passed through to the
app). Set `DJ_PUBLIC_URL=https://<name>:8448` in the app's `.env` and use that URL, port
included, for the Google redirect URI. The TLS-ALPN challenge needs public 443, so the
certificate needs either the HTTP-01 challenge (router 80 -> CT116 80) or the DNS challenge
above (DuckDNS: `github.com/caddy-dns/duckdns`, `dns duckdns {env.DUCKDNS_TOKEN}`).

## Network path for exposure (owner does this, later)

1. Router: forward TCP 80 and 443 -> Proxmox host `192.168.1.2`. Nothing else.
2. Proxmox host: DNAT on `vmbr0` only, same style as the existing rules:

   ```bash
   iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 80  -j DNAT --to-destination 10.0.0.116:80
   iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport 443 -j DNAT --to-destination 10.0.0.116:443
   ```

   No rule for the console port (8000) or the stem port: those stay on 127.0.0.1 in CT116.
3. CT116 firewall: inbound 80/443 only; 8000 and the stem port are not reachable from
   outside the container (they are bound to 127.0.0.1 anyway).
4. Before opening the router: Google sign-in configured (or an owner password), `DJ_SESSION_SECRET` in `.env`,
   `DJ_PUBLIC_URL` in `.env` (with no login method set, it makes every request answer 503),
   `curl -I https://$DJ_DOMAIN/health` answers 200 and `curl https://$DJ_DOMAIN/api/recipes`
   answers 401.
