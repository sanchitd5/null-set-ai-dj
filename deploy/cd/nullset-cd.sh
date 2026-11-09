#!/usr/bin/env bash
# nullset-cd: pull-based continuous deploy for the web server on CT116.
#
#   nullset-cd.sh [--dry-run] [--once SHA]
#
# Every run (systemd timer, every 3 min):
#   1. asks GitHub (unauthenticated REST, public repo) for the newest main commit whose ci.yml run succeeded;
#   2. does nothing unless that commit is ahead of HEAD (the deploy branch) and HEAD is its ancestor;
#   3. tags pre-cd-<ts>, fast-forwards (never stash/reset local edits), pip-installs if requirements.txt changed,
#      restarts only the services whose code changed, smoke-checks, and rolls back on any failure.
# --dry-run : print the plan, change nothing (a `git fetch` still updates remote-tracking objects).
# --once SHA: deploy that specific commit, which must be one of the green main commits.
# Kill switch: touch /etc/nullset-cd.disabled.
#
# Exit codes: 0 deployed / nothing to do / API unavailable, 1 refused (diverged, local edits, bad config),
# 3 deploy failed and was rolled back, 2 rollback itself failed (needs a human).
#
# Everything below is overridable from the environment (the tests do, a stock CT116 needs none of it).
set -euo pipefail

APP_DIR="${NULLSET_CD_APP_DIR:-/opt/null-set-ai-dj}"
BRANCH="${NULLSET_CD_BRANCH:-deploy-parity}"
UPSTREAM_BRANCH="${NULLSET_CD_UPSTREAM_BRANCH:-main}"
REMOTE="${NULLSET_CD_REMOTE:-origin}"
REPO="${NULLSET_CD_REPO:-sanchitd5/null-set-ai-dj}"
API="${NULLSET_CD_API:-https://api.github.com}"
WORKFLOW="${NULLSET_CD_WORKFLOW:-ci.yml}"
VENV="${NULLSET_CD_VENV:-$APP_DIR/.venv}"
HEALTH_URL="${NULLSET_CD_HEALTH_URL:-http://127.0.0.1:8000/health}"
WEB_SERVICE="${NULLSET_CD_WEB_SERVICE:-nullset-dj}"
STEM_SERVICE="${NULLSET_CD_STEM_SERVICE:-stem-server}"
STATE_DIR="${NULLSET_CD_STATE_DIR:-/var/lib/nullset-cd}"
LOG_FILE="${NULLSET_CD_LOG_FILE:-/var/log/nullset-cd.log}"
LOCK_FILE="${NULLSET_CD_LOCK_FILE:-/run/nullset-cd.lock}"
DISABLE_FILE="${NULLSET_CD_DISABLE_FILE:-/etc/nullset-cd.disabled}"
KEEP_TAGS="${NULLSET_CD_KEEP_TAGS:-20}"
SETTLE_SECONDS="${NULLSET_CD_SETTLE_SECONDS:-10}"     # wait after a restart before the "service active" check
HEALTH_SECONDS="${NULLSET_CD_HEALTH_SECONDS:-60}"     # health route must answer 200 within this
HEALTH_POLL="${NULLSET_CD_HEALTH_POLL:-2}"

DRY=0 ONCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --once) ONCE="${2:-}"; [ -n "$ONCE" ] || { echo "nullset-cd: --once needs a sha" >&2; exit 1; }; shift 2 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "nullset-cd: unknown argument: $1" >&2; exit 1 ;;
  esac
done

START=$SECONDS
SHORT_SHA="-"
say() { echo "nullset-cd: $*"; }                      # stdout -> journal (SyslogIdentifier=nullset-cd)
record() {                                            # one line per decision that matters: ts sha result duration
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $SHORT_SHA $1 $((SECONDS - START))s"
  if [ "$DRY" = 1 ]; then return 0; fi
  { mkdir -p "$(dirname "$LOG_FILE")" && echo "$line" >> "$LOG_FILE"; } 2>/dev/null || say "cannot write $LOG_FILE"
}

if [ -e "$DISABLE_FILE" ]; then say "disabled by $DISABLE_FILE"; exit 0; fi

# git/pip run as the checkout's owner when the timer runs as root (no root-owned files in the tree).
OWNER="$(stat -c %U "$APP_DIR" 2>/dev/null || true)"
run_owner() {
  if [ "$(id -u)" = 0 ] && [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    runuser -u "$OWNER" -- env HOME="$(getent passwd "$OWNER" | cut -d: -f6)" "$@"
  else
    "$@"
  fi
}
g() { run_owner git -C "$APP_DIR" "$@"; }

[ -d "$APP_DIR/.git" ] || { say "no git checkout at $APP_DIR"; exit 1; }

# ---- lock: two runs never overlap (a second one just leaves)
if [ "$DRY" = 0 ]; then
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then say "another run holds the lock, leaving"; exit 0; fi
fi

# ---- 1. newest green main commit
runs_json="$(mktemp)"; trap 'rm -f "$runs_json"' EXIT
url="$API/repos/$REPO/actions/workflows/$WORKFLOW/runs?branch=$UPSTREAM_BRANCH&event=push&status=success&per_page=30"
if ! curl -fsS --max-time 30 -H 'Accept: application/vnd.github+json' -o "$runs_json" "$url" 2>"$runs_json.err"; then
  say "GitHub API unavailable or rate-limited ($(tr '\n' ' ' < "$runs_json.err" | cut -c1-160)); doing nothing"
  rm -f "$runs_json.err"; exit 0
fi
rm -f "$runs_json.err"
# newest first; only completed+success runs of the workflow on the upstream branch
green_list="$(python3 - "$runs_json" "$UPSTREAM_BRANCH" <<'PY'
import json, sys
try:
    runs = json.load(open(sys.argv[1])).get("workflow_runs", [])
except Exception:
    sys.exit(3)
ok = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"
      and r.get("head_branch") == sys.argv[2] and r.get("head_sha")]
ok.sort(key=lambda r: r.get("created_at") or "", reverse=True)
for r in ok:
    print(r["head_sha"])
PY
)" || { say "unreadable GitHub answer; doing nothing"; exit 0; }
if [ -z "$green_list" ]; then say "no green $UPSTREAM_BRANCH run yet; doing nothing"; exit 0; fi

if [ -n "$ONCE" ]; then
  target="$(printf '%s\n' "$green_list" | grep -m1 -E "^${ONCE}" || true)"
  if [ -z "$target" ]; then SHORT_SHA="${ONCE:0:9}"; say "--once $ONCE is not a green $UPSTREAM_BRANCH commit"; record "REFUSED not-green"; exit 1; fi
else
  target="$(printf '%s\n' "$green_list" | head -n1)"
fi
SHORT_SHA="${target:0:9}"

cur_branch="$(g symbolic-ref --short -q HEAD || true)"
if [ "$cur_branch" != "$BRANCH" ]; then
  say "checkout is on '${cur_branch:-detached HEAD}', not $BRANCH; refusing"; record "REFUSED wrong-branch"; exit 1
fi
head="$(g rev-parse HEAD)"

# ---- 2. compare with HEAD
if [ "$target" = "$head" ]; then exit 0; fi                       # quiet: already deployed
if ! g fetch --quiet "$REMOTE" "$UPSTREAM_BRANCH" 2>/dev/null; then say "git fetch failed; doing nothing"; exit 0; fi
if ! g cat-file -e "${target}^{commit}" 2>/dev/null; then say "$SHORT_SHA not fetched (not on $UPSTREAM_BRANCH?); doing nothing"; exit 0; fi
if ! g merge-base --is-ancestor "$target" FETCH_HEAD; then
  say "green $SHORT_SHA is not on $REMOTE/$UPSTREAM_BRANCH; refusing"; record "REFUSED not-on-main"; exit 1
fi
if g merge-base --is-ancestor "$target" "$head"; then
  [ -n "$ONCE" ] && say "$SHORT_SHA is already contained in HEAD"; exit 0     # prod is ahead of the newest green: nothing to do
fi
if ! g merge-base --is-ancestor "$head" "$target"; then
  say "DIVERGED: HEAD ${head:0:9} is not an ancestor of green $SHORT_SHA; refusing to deploy"
  record "REFUSED diverged"; exit 1
fi
if [ -z "$ONCE" ] && [ -f "$STATE_DIR/bad_sha" ] && [ "$(cat "$STATE_DIR/bad_sha")" = "$target" ]; then
  say "$SHORT_SHA failed before and was rolled back; waiting for a newer green commit"; exit 0
fi

# ---- plan: what changed decides what happens
changed="$(g diff --name-only "$head" "$target")"
need_pip=0 need_web=0 need_stem=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    requirements.txt) need_pip=1; need_web=1; need_stem=1 ;;
    start.sh) need_web=1 ;;
    app/tests/*) ;;
    app/stem_server/*|app/stem_server.py) need_stem=1 ;;
    app/*) need_web=1 ;;                                              # everything else under app/ is the console
  esac
  case "$f" in                                                        # what the stem server imports from the console tree
    app/music_brain/audio/stem_service.py|app/music_brain/config.py|app/ui/services/auth.py) need_stem=1 ;;
  esac
done <<< "$changed"
services=()
[ "$need_web" = 1 ] && services+=("$WEB_SERVICE")
[ "$need_stem" = 1 ] && services+=("$STEM_SERVICE")
say "plan: ${head:0:9} -> $SHORT_SHA, $(printf '%s\n' "$changed" | grep -c . || true) files, pip=$need_pip, restart=[${services[*]:-nothing}]"
if [ "$DRY" = 1 ]; then say "dry run: nothing changed"; exit 0; fi

# ---- 3/4. tag, fast-forward
ts="$(date -u +%Y%m%d-%H%M%S)"; tag="pre-cd-$ts"
n=1; while g rev-parse -q --verify "refs/tags/$tag" >/dev/null; do n=$((n + 1)); tag="pre-cd-$ts-$n"; done   # two runs in one second
g tag "$tag" "$head"
if ! ff_out="$(g merge --ff-only "$target" 2>&1)"; then
  g tag -d "$tag" >/dev/null 2>&1 || true
  say "REFUSED: fast-forward failed (local edits in the way?): $(printf '%s' "$ff_out" | tr '\n' ' ' | cut -c1-300)"
  record "REFUSED ff-failed"; exit 1
fi
say "fast-forwarded ${head:0:9} -> $SHORT_SHA (rollback tag $tag)"

# The knowledge folder is the private dj-atlas submodule (docs/engineering/ATLAS_REPO.md). Create it when it is
# missing (first deploy after the split, a fresh checkout); NEVER `submodule update` an existing one: the server's
# own publishes move it ahead of the pin, and an update would roll its learnings back.
ATLAS_PATH="app/music_brain/knowledge"
if [ -f "$APP_DIR/.gitmodules" ] && grep -q "path = $ATLAS_PATH" "$APP_DIR/.gitmodules" && [ ! -e "$APP_DIR/$ATLAS_PATH/.git" ]; then
  atlas_url="${DJ_ATLAS_REMOTE_URL:-git@github.com:sanchitd5/dj-atlas.git}"
  atlas_key="${DJ_ATLAS_SSH_KEY:-/root/.ssh/dj-atlas-deploy}"
  g config "submodule.$ATLAS_PATH.url" "$atlas_url"
  if GIT_SSH_COMMAND="ssh -i $atlas_key -o IdentitiesOnly=yes -o BatchMode=yes" run_owner git -C "$APP_DIR" submodule update --init -- "$ATLAS_PATH" >/dev/null 2>&1; then
    say "knowledge submodule created from $atlas_url"
  else
    say "knowledge submodule could not be created from $atlas_url"; deploy_fail_atlas=1
  fi
fi

smoke() {                                                             # $1 = epoch of the restart; checks the given services
  local since="$1" svc waited=0 code
  shift
  if [ "$#" -gt 0 ]; then sleep "$SETTLE_SECONDS"; fi
  for svc in "$@"; do
    systemctl is-active --quiet "$svc" || { say "smoke: $svc is not active"; return 1; }
  done
  while :; do
    code="$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || true)"
    [ "$code" = 200 ] && break
    waited=$((waited + HEALTH_POLL))
    if [ "$waited" -gt "$HEALTH_SECONDS" ]; then say "smoke: $HEALTH_URL answered '${code:-none}', not 200"; return 1; fi
    sleep "$HEALTH_POLL"
  done
  for svc in "$@"; do
    if journalctl -u "$svc" --since "@$since" --no-pager -q 2>/dev/null | grep -q Traceback; then
      say "smoke: Traceback in the journal of $svc since the restart"; return 1
    fi
  done
  return 0
}

restart_all() { local svc; for svc in "${services[@]}"; do systemctl restart "$svc" || return 1; done; }

deploy_fail=""
[ -n "${deploy_fail_atlas:-}" ] && deploy_fail="knowledge submodule missing"
if [ "$need_pip" = 1 ]; then
  say "requirements.txt changed: pip install"
  run_owner "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" 2>&1 | tail -n 5 || deploy_fail="pip install failed"   # pipefail: pip's status wins
fi
restart_epoch="$(date +%s)"
if [ -z "$deploy_fail" ] && [ "${#services[@]}" -gt 0 ]; then       # docs/tests-only: no restart, no smoke
  restart_all || deploy_fail="systemctl restart failed"
fi
if [ -z "$deploy_fail" ] && [ "${#services[@]}" -gt 0 ]; then
  smoke "$restart_epoch" "${services[@]}" || deploy_fail="smoke check failed"
fi

if [ -z "$deploy_fail" ]; then
  mkdir -p "$STATE_DIR" 2>/dev/null && rm -f "$STATE_DIR/bad_sha" || true
  # keep the newest KEEP_TAGS rollback tags
  g tag -l 'pre-cd-*' | sort | head -n -"$KEEP_TAGS" | while IFS= read -r old; do g tag -d "$old" >/dev/null 2>&1 || true; done
  say "DEPLOYED $SHORT_SHA restarted=[${services[*]:-nothing}]"
  record "DEPLOYED restarted=${services[*]:-none}"
  exit 0
fi

# ---- 8. rollback
say "FAILED ($deploy_fail); rolling back to $tag"
g reset --hard "$tag" >/dev/null
rb_fail=""
if [ "$need_pip" = 1 ]; then
  run_owner "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" 2>&1 | tail -n 5 || rb_fail="pip reinstall failed"
fi
restart_epoch="$(date +%s)"
if [ "${#services[@]}" -gt 0 ]; then
  restart_all || rb_fail="${rb_fail:+$rb_fail; }restart failed"
  smoke "$restart_epoch" "${services[@]}" || rb_fail="${rb_fail:+$rb_fail; }smoke failed"
fi
mkdir -p "$STATE_DIR" 2>/dev/null && echo "$target" > "$STATE_DIR/bad_sha" || say "cannot write $STATE_DIR/bad_sha"
if [ -n "$rb_fail" ]; then
  say "ROLLBACK FAILED for $SHORT_SHA ($rb_fail): needs a human"
  record "ROLLBACK-FAILED $deploy_fail; $rb_fail"; exit 2
fi
say "ROLLED BACK $SHORT_SHA ($deploy_fail)"
record "ROLLED BACK $deploy_fail"
exit 3
