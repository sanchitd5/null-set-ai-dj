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
set -euo pipefail

HOST_PORT=2298 HOST=root@192.168.1.2 CT=116
DEST=/opt/null-set-ai-dj/data/cache/app
DRY=0 NAME="" CODE="" PUBSPEC="" OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --build-name) NAME="${2:?}"; shift 2 ;;
    --build-number) CODE="${2:?}"; shift 2 ;;
    --pubspec) PUBSPEC="${2:?}"; shift 2 ;;
    --out-dir) OUT="${2:?}"; shift 2 ;;
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

SHA=$(shasum -a 256 "$APK" | awk '{print $1}')
SIZE=$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")
OUT="${OUT:-$(mktemp -d)}"; JSON="$OUT/null-set-dj.json"
NAME="$NAME" CODE="$CODE" SHA="$SHA" SIZE="$SIZE" NOTES="$NOTES" TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" python3 - "$JSON" <<'PY'
import json, os, sys
json.dump({"version_name": os.environ["NAME"], "version_code": int(os.environ["CODE"]), "sha256": os.environ["SHA"],
           "size": int(os.environ["SIZE"]), "notes": os.environ["NOTES"], "published_at": os.environ["TS"]},
          open(sys.argv[1], "w"), indent=2)
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
H
)
HOSTSCRIPT="${HOSTSCRIPT//__DEST__/$DEST}"; HOSTSCRIPT="${HOSTSCRIPT//__CT__/$CT}"

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
run scp -P $HOST_PORT "$APK" "$HOST:/root/null-set-dj.apk"
run scp -P $HOST_PORT "$JSON" "$HOST:/root/null-set-dj.json"
if [ "$DRY" = 1 ]; then
  echo "[dry-run] ssh -p $HOST_PORT $HOST bash -s $SHA <<'script'"; echo "$HOSTSCRIPT"; echo "script"
else
  ssh -p $HOST_PORT "$HOST" bash -s "$SHA" <<<"$HOSTSCRIPT"
fi
