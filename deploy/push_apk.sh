#!/bin/bash
# Publish an Android build for the in-app updater. Run on the Mac that built the APK.
#
#   deploy/push_apk.sh [--dry-run] --build-name 1.0.0-parity.3 --build-number 4 APK "notes"
#   deploy/push_apk.sh [--dry-run] --pubspec ../null-set-dj-mobile/pubspec.yaml APK "notes"
#
# Version source (the phone compares version_code with its own build number):
#   1. --build-name/--build-number: the values you passed to `flutter build apk`. Preferred, no guessing.
#   2. --pubspec FILE: parses `version: NAME+CODE`. Only right if you built without overriding them.
# Writes null-set-dj.json (version, sha256, size, notes, published_at), copies APK + JSON to the
# Proxmox host, pushes both into CT116 (previous files kept as .bak with plain cp: the container
# refuses `cp -p`) and checks the sha256 inside CT116.
# Signing: updates only install over the same signing key. Build every APK on the same Mac
# (debug keystore today); a switch to a real release key needs a one-time uninstall.
# --dry-run prints the remote commands and touches nothing remote. --out-dir: where the JSON is written.
# Delta updates: after the push, deploy/apk_delta.py (stdlib python3, nothing to install) runs INSIDE the CT:
# it archives the APK as history/<code>.apk, keeps the last N (--keep N, default $APK_KEEP or 3), builds
# patches/<from>.patch from each older kept APK to the new one and lists them under "patches" in
# null-set-dj.json (from_version_code, from_sha256, sha256 of the result APK, patch_sha256, patch_size, size).
# The phone then downloads a few MB instead of the whole APK. --no-patches skips this step.
# Needs python3 in the CT and ~3x the APK size of free RAM for a few seconds.
# Release notes: the same push also appends this build's NOTES to changelog.json next to the APK (deploy/changelog.py;
# idempotent per build number, older entries kept), which the app's "What's new" page reads from /api/app/changelog.
# One-time history seed: deploy/push_changelog_seed.sh.
# Force update: --block 15,16 lists builds that must update ("blocked_version_codes" in android.json): a phone on one of
# them skips its normal start and opens the update screen. Without --block the earlier list is kept (apk_delta.py
# carries it over, so not with --no-patches); --block none clears it. Block only builds older than the one you publish.
set -euo pipefail

HOST_PORT="${HOST_PORT:-2298}" HOST="${HOST:-pve}" CT="${CT:-116}"   # pve = ssh alias (~/.ssh/config); override HOST/HOST_PORT/CT from env
DEST=/opt/null-set-ai-dj/data/cache/app
DRY=0 NAME="" CODE="" PUBSPEC="" OUT="" KEEP="${APK_KEEP:-3}" PATCHES=1 BLOCK=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --keep) KEEP="${2:?}"; shift 2 ;;
    --no-patches) PATCHES=0; shift ;;
    --build-name) NAME="${2:?}"; shift 2 ;;
    --build-number) CODE="${2:?}"; shift 2 ;;
    --pubspec) PUBSPEC="${2:?}"; shift 2 ;;
    --out-dir) OUT="${2:?}"; shift 2 ;;
    --block) BLOCK="${2:?}"; shift 2 ;;
    --) shift; break ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
APK="${1:?usage: push_apk.sh [opts] APK NOTES}"; NOTES="${2:?notes}"
[ -f "$APK" ] || { echo "no such file: $APK" >&2; exit 1; }

if [ -z "$NAME" ] || [ -z "$CODE" ]; then
  [ -n "$PUBSPEC" ] || { echo "give --build-name and --build-number (preferred) or --pubspec" >&2; exit 2; }
  V=$(sed -n 's/^version:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$PUBSPEC" | head -1)
  case "$V" in *+*) ;; *) echo "pubspec version '$V' has no +build" >&2; exit 1 ;; esac
  NAME="${NAME:-${V%%+*}}"; CODE="${CODE:-${V##*+}}"
  echo "version from pubspec: $NAME+$CODE"
else
  echo "version from build args: $NAME+$CODE"
fi
case "$CODE" in ''|*[!0-9]*) echo "build number must be an integer: $CODE" >&2; exit 1 ;; esac
case "$KEEP" in ''|*[!0-9]*|0) echo "--keep must be a positive integer: $KEEP" >&2; exit 1 ;; esac
case "$BLOCK" in ''|none) ;; *[!0-9,]*|,*|*,|*,,*) echo "--block takes build numbers like 15,16 (or 'none'): $BLOCK" >&2; exit 1 ;; esac

if command -v sha256sum >/dev/null 2>&1; then SHA=$(sha256sum "$APK" | awk '{print $1}'); else SHA=$(shasum -a 256 "$APK" | awk '{print $1}'); fi
SIZE=$(stat -c %s "$APK" 2>/dev/null || stat -f %z "$APK")
OUT="${OUT:-$(mktemp -d)}"; JSON="$OUT/null-set-dj.json"
NAME="$NAME" CODE="$CODE" SHA="$SHA" SIZE="$SIZE" NOTES="$NOTES" BLOCK="$BLOCK" TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" python3 - "$JSON" <<'PY'
import json, os, sys
meta = {"version_name": os.environ["NAME"], "version_code": int(os.environ["CODE"]), "sha256": os.environ["SHA"],
        "size": int(os.environ["SIZE"]), "notes": os.environ["NOTES"], "published_at": os.environ["TS"]}
if os.environ["BLOCK"]:   # --block: "none" = an empty list (clears the earlier list), else the codes
    meta["blocked_version_codes"] = [] if os.environ["BLOCK"] == "none" else [int(c) for c in os.environ["BLOCK"].split(",")]
json.dump(meta, open(sys.argv[1], "w"), indent=2)
PY
echo "wrote $JSON (sha256 $SHA, $SIZE bytes)"

# Host-side script: runs on Proxmox, so `pct` is available.
HOSTSCRIPT=$(cat <<'H'
set -euo pipefail
SHA="$1"; D=__DEST__
pct exec __CT__ -- mkdir -p "$D"
for f in null-set-dj.apk null-set-dj.json; do
  if pct exec __CT__ -- test -f "$D/$f"; then pct exec __CT__ -- cp "$D/$f" "$D/$f.bak"; fi
  pct push __CT__ "/root/$f" "$D/$f" --perms 644
  rm -f "/root/$f"
done
GOT=$(pct exec __CT__ -- sha256sum "$D/null-set-dj.apk" | awk '{print $1}')
[ "$GOT" = "$SHA" ] && echo "verified sha256 in CT__CT__: $GOT" || { echo "SHA MISMATCH in CT: $GOT" >&2; exit 1; }
if [ "$2" = 1 ]; then
  # delta patches: archive this APK, keep the last $3, build old->new patches, list them in the json
  pct push __CT__ /root/apk_delta.py "$D/apk_delta.py" --perms 755
  pct exec __CT__ -- python3 "$D/apk_delta.py" publish --dir "$D" --keep "$3"
fi
rm -f /root/apk_delta.py
# release-notes history (GET /api/app/changelog): add this build's entry; a failure here must not undo the release
pct push __CT__ /root/changelog.py "$D/changelog.py" --perms 755
pct exec __CT__ -- python3 "$D/changelog.py" add --dir "$D" || echo "WARNING: changelog entry not written" >&2
rm -f /root/changelog.py
H
)
HOSTSCRIPT="${HOSTSCRIPT//__DEST__/$DEST}"; HOSTSCRIPT="${HOSTSCRIPT//__CT__/$CT}"

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
run scp -P $HOST_PORT "$APK" "$HOST:/root/null-set-dj.apk"
run scp -P $HOST_PORT "$JSON" "$HOST:/root/null-set-dj.json"
[ "$PATCHES" = 1 ] && run scp -P $HOST_PORT "$HERE/apk_delta.py" "$HOST:/root/apk_delta.py"
run scp -P $HOST_PORT "$HERE/changelog.py" "$HOST:/root/changelog.py"
if [ "$DRY" = 1 ]; then
  echo "[dry-run] ssh -p $HOST_PORT $HOST bash -s $SHA $PATCHES $KEEP <<'script'"; echo "$HOSTSCRIPT"; echo "script"
else
  ssh -p $HOST_PORT "$HOST" bash -s "$SHA" "$PATCHES" "$KEEP" <<<"$HOSTSCRIPT"
fi
