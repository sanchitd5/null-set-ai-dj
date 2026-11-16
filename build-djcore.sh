#!/usr/bin/env bash
# Build the dj-core decision service (the exact commit this server pins) for running the web locally.
#
#   ./build-djcore.sh          build deploy/dj-core-svc.version's sha into .djcore/bin/<sha> (skipped when already built)
#                              and print its path
#   ./build-djcore.sh --path   print the path of the pinned build if it exists (exit 1 when it does not); builds nothing
#   ./build-djcore.sh --sha    print the pinned sha
#
#   DJ_CORE_DIR=/path/to/dj-core   the dj-core checkout (default ../dj-core next to this repo; cloned there when missing)
#   DJ_CORE_REPO=<git url>         where to clone it from (default git@github.com:sanchitd5/dj-core.git, needs your GitHub access)
#   DART=/path/to/dart             the Dart SDK binary (default: `dart` on PATH, else the one bundled with flutter)
#
# start.sh uses the result (DJ_CORE_SVC) when the server's own install path does not exist, so on a Mac this is the
# only step needed before ./start.sh. Works with macOS bash 3.2 and Linux.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PIN_FILE="${DJ_CORE_PIN_FILE:-$ROOT/deploy/dj-core-svc.version}"
OUT_DIR="${DJ_CORE_OUT_DIR:-$ROOT/.djcore/bin}"

[[ -f "$PIN_FILE" ]] || { echo "build-djcore: no pin file at $PIN_FILE" >&2; exit 1; }
SHA="$(sed -n 's/^sha=//p' "$PIN_FILE" | head -n 1 | tr -d '[:space:]')"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "build-djcore: $PIN_FILE has no valid sha= line" >&2; exit 1; }
BIN="$OUT_DIR/$SHA"

case "${1:-}" in
  --sha) echo "$SHA"; exit 0 ;;
  --path) [[ -x "$BIN" ]] && { echo "$BIN"; exit 0; }; exit 1 ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (see ./build-djcore.sh --help)" >&2; exit 2 ;;
esac

if [[ -x "$BIN" ]]; then echo "$BIN"; exit 0; fi

# ---- a Dart SDK --------------------------------------------------------------
find_dart() {
  if [[ -n "${DART:-}" ]]; then echo "$DART"; return 0; fi
  if command -v dart >/dev/null 2>&1; then command -v dart; return 0; fi
  if command -v flutter >/dev/null 2>&1; then
    local f d
    f="$(command -v flutter)"
    while [[ -L "$f" ]]; do      # flutter is often a symlink into its SDK (Homebrew, ~/bin)
      d="$(readlink "$f")"
      case "$d" in /*) f="$d" ;; *) f="$(dirname "$f")/$d" ;; esac
    done
    d="$(cd "$(dirname "$f")" && pwd)/cache/dart-sdk/bin/dart"
    [[ -x "$d" ]] && { echo "$d"; return 0; }
  fi
  return 1
}
DART_BIN="$(find_dart)" || {
  echo "build-djcore: no Dart SDK found (no 'dart' on PATH, no flutter with a bundled dart)." >&2
  echo "Install one, then re-run ./build-djcore.sh:   brew install dart-sdk   (or install flutter, or set DART=/path/to/dart)" >&2
  exit 1
}

# ---- the dj-core checkout ------------------------------------------------------
CORE="${DJ_CORE_DIR:-$ROOT/../dj-core}"
if [[ ! -d "$CORE/.git" ]]; then
  REPO="${DJ_CORE_REPO:-git@github.com:sanchitd5/dj-core.git}"
  echo "build-djcore: cloning $REPO into $CORE" >&2
  git clone "$REPO" "$CORE" >&2 || { echo "build-djcore: clone failed (private repo: needs your GitHub SSH access)" >&2; exit 1; }
fi
git -C "$CORE" fetch --quiet origin >&2 || echo "build-djcore: git fetch failed (offline?), trying with what the checkout has" >&2
git -C "$CORE" cat-file -e "$SHA^{commit}" 2>/dev/null || {
  echo "build-djcore: commit $SHA is not in $CORE even after fetch" >&2; exit 1; }

# ---- build that exact sha in a temp worktree -------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/djcore-build.XXXXXX")"
cleanup() { git -C "$CORE" worktree remove --force "$TMP/src" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
git -C "$CORE" worktree add --detach --quiet "$TMP/src" "$SHA" >&2
echo "build-djcore: building dj-core ${SHA:0:9} with $DART_BIN (takes a minute)" >&2
(cd "$TMP/src" && "$DART_BIN" pub get >&2 && "$DART_BIN" compile exe bin/serve.dart -o "$TMP/dj-core-svc" >&2) || {
  echo "build-djcore: dart build failed (see above)" >&2; exit 1; }
mkdir -p "$OUT_DIR"
cp "$TMP/dj-core-svc" "$BIN.tmp" && chmod 755 "$BIN.tmp" && mv "$BIN.tmp" "$BIN"
echo "$BIN"
