# nullset-cd: pull deploy for the web server (CT116)

A systemd timer runs `nullset-cd.sh` every 3 minutes on CT116. No inbound access, no runner, no credentials:
it reads the public GitHub API (unauthenticated, 60 requests/h; one request per run = 20/h).

Per run: newest `main` commit whose `ci.yml` run succeeded -> if it is ahead of the checkout and the checkout
is its ancestor -> tag `pre-cd-<ts>`, `git merge --ff-only`, `pip install -r requirements.txt` only if that file
changed, restart only what changed, smoke check, roll back on any failure.

## What gets restarted

| changed path | restart |
|---|---|
| `requirements.txt` | pip install, then both services |
| `start.sh`, anything under `app/` except the rows below | `nullset-dj` |
| `app/stem_server/*` | `stem-server` |
| `app/music_brain/audio/stem_service.py`, `app/music_brain/config.py`, `app/ui/services/auth.py` (the stem server imports them) | `stem-server` (and `nullset-dj`, they are under `app/`) |
| `app/tests/*`, docs, deploy/, workflows, everything else | nothing (no smoke either) |

## Safety

- Not green, pending, API error, rate limit: nothing happens (one log line).
- Checkout not on `deploy-parity`, or HEAD not an ancestor of the green commit (prod diverged): refuses, exit 1.
- Local edits that the fast-forward would touch: `--ff-only` fails, nothing is changed, exit 1. Local edits are never stashed or reset.
- Smoke: each restarted service active after 10 s, `http://127.0.0.1:8000/health` = 200 within 60 s, no `Traceback` in the restarted services' journal since the restart.
- Failure after the fast-forward: `git reset --hard pre-cd-<ts>`, pip again if requirements changed, restart the same services, smoke again, log `ROLLED BACK <sha>` (exit 3). The bad sha is written to `/var/lib/nullset-cd/bad_sha` and not retried until a different green commit appears. If the rollback smoke fails too: `ROLLBACK FAILED` (exit 2), needs a human.
- Two runs never overlap (`flock` on `/run/nullset-cd.lock`).
- The last 20 `pre-cd-*` tags are kept.

## Install (the lead; the script is NOT installed by the PR)

Run on the Proxmox host, with the repo checked out at the commit that contains `deploy/cd/`
(or copy the four files from a clone of `main`):

    cd deploy/cd
    pct push 116 nullset-cd.sh /usr/local/sbin/nullset-cd.sh --perms 0755
    pct push 116 nullset-cd.service /etc/systemd/system/nullset-cd.service
    pct push 116 nullset-cd.timer /etc/systemd/system/nullset-cd.timer
    pct exec 116 -- bash -c 'bash -n /usr/local/sbin/nullset-cd.sh && /usr/local/sbin/nullset-cd.sh --dry-run'
    pct exec 116 -- systemctl daemon-reload
    pct exec 116 -- systemctl enable --now nullset-cd.timer

First install: the script file in `/opt/null-set-ai-dj/deploy/cd/` only exists after prod has the commit, so push it
from outside as above. A later change to the script needs the same re-push (the deploy does not update `/usr/local/sbin`).
Needs `git`, `python3`, `curl`, `flock`, `runuser`, `journalctl` in CT116 (all Debian base).
`StateDirectory=nullset-cd` creates `/var/lib/nullset-cd`.

## Operate

    systemctl list-timers nullset-cd.timer
    journalctl -t nullset-cd -n 50          # decisions
    tail /var/log/nullset-cd.log            # one line per deploy / refusal / rollback: ts sha result duration
    /usr/local/sbin/nullset-cd.sh --dry-run             # plan only (a git fetch still happens)
    /usr/local/sbin/nullset-cd.sh --once <green-sha>    # deploy that green main commit now
    touch /etc/nullset-cd.disabled          # kill switch: every run does nothing
    rm /etc/nullset-cd.disabled             # back on
    systemctl disable --now nullset-cd.timer   # off entirely

Manual rollback: `git -C /opt/null-set-ai-dj tag -l 'pre-cd-*'`, `git reset --hard <tag>`, restart the services,
and `touch /etc/nullset-cd.disabled` first or the next run deploys the same commit again.

Environment overrides (`NULLSET_CD_*`: APP_DIR, BRANCH, REPO, API, HEALTH_URL, STATE_DIR, LOG_FILE, ...) are listed at the
top of the script; the tests use them.

## dj-core service executable (not part of nullset-cd.sh yet)

All pick decisions run in the dj-core service `dj-core-svc` (see `app/music_brain/djcore_bridge.py`). It is built from the private
dj-core repo, so this public repo only pins it: `deploy/dj-core-svc.pin` = `DJ_CORE_SHA` (the dj-core commit) and
`DJ_CORE_SHA256` (of the exe). dj-core CI publishes `dj-core-svc-<sha>` and `dj-core-svc-<sha>.sha256` as assets of the release
`svc-<sha>` on every green main. The server starts the exe from `DJ_CORE_SVC` (default `/opt/null-set-ai-dj/bin/dj-core-svc`).

Fetch step for the CD script (to add later; the script is not changed by this PR). The token is a read-only token for the
dj-core repo that the owner places on CT116 (`/etc/nullset-cd/dj-core.token`, mode 0600); nobody else handles it:

    . /opt/null-set-ai-dj/deploy/dj-core-svc.pin
    cur=$(sha256sum /opt/null-set-ai-dj/bin/dj-core-svc 2>/dev/null | cut -d' ' -f1)
    if [ -n "$DJ_CORE_SHA" ] && [ "$cur" != "$DJ_CORE_SHA256" ]; then
      GH_TOKEN=$(cat /etc/nullset-cd/dj-core.token) gh release download "svc-$DJ_CORE_SHA" --repo sanchitd5/dj-core \
        --pattern "dj-core-svc-$DJ_CORE_SHA" --output /opt/null-set-ai-dj/bin/dj-core-svc.new --clobber
      echo "$DJ_CORE_SHA256  /opt/null-set-ai-dj/bin/dj-core-svc.new" | sha256sum -c - \
        && chmod 0755 /opt/null-set-ai-dj/bin/dj-core-svc.new && mv -f /opt/null-set-ai-dj/bin/dj-core-svc.new /opt/null-set-ai-dj/bin/dj-core-svc
    fi

Restart `nullset-dj` after a changed exe (the bridge keeps the old child alive). A pin bump rolls back together with the server
checkout: `git reset --hard pre-cd-<ts>` restores the previous pin, then the same fetch step restores the previous exe.
Without the exe the bridge raises `DjCoreUnavailable` and logs `[djcore] ERROR ...`; there is no Python fallback.
