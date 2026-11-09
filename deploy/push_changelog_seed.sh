#!/bin/bash
# One-time (safe to repeat): merge deploy/changelog_seed.json into the release-notes history in CT116.
#   deploy/push_changelog_seed.sh [--dry-run]
# Only adds builds that are not in the history yet (never overwrites), so it can run before or after push_apk.sh.
set -euo pipefail
HOST_PORT="${HOST_PORT:-2298}" HOST="${HOST:-pve}" CT="${CT:-116}"
DEST=/opt/null-set-ai-dj/data/cache/app
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
run scp -P $HOST_PORT "$HERE/changelog.py" "$HERE/changelog_seed.json" "$HOST:/root/"
REMOTE="set -euo pipefail
pct exec $CT -- mkdir -p $DEST
pct push $CT /root/changelog.py $DEST/changelog.py --perms 755
pct push $CT /root/changelog_seed.json $DEST/changelog_seed.json --perms 644
pct exec $CT -- python3 $DEST/changelog.py seed --dir $DEST --file $DEST/changelog_seed.json
rm -f /root/changelog.py /root/changelog_seed.json"
if [ "$DRY" = 1 ]; then echo "[dry-run] ssh -p $HOST_PORT $HOST bash -s <<<"; echo "$REMOTE"; else ssh -p $HOST_PORT "$HOST" bash -s <<<"$REMOTE"; fi
