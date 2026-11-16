# Sourced by start.sh: make sure the dj-core decision service is reachable. bash 3.2 compatible.
#
#   resolve_djcore   exports DJ_CORE_SVC when the pinned prod install path is missing and a local build exists (building it once
#                    with ./build-djcore.sh when it does not). Returns 1, with the exact command to run, when there is no service.
#
# Inputs (all optional): DJ_CORE_SVC (set = nothing to do), DJ_CORE_VERSIONS_DIR (default /opt/null-set-ai-dj/bin/versions),
# DJ_CORE_PIN_FILE, DJC_BUILD (the build script, default ./build-djcore.sh), DJ_CORE_SKIP=1 (run without the service on purpose).
resolve_djcore() {
  local pin="${DJ_CORE_PIN_FILE:-deploy/dj-core-svc.version}" vdir="${DJ_CORE_VERSIONS_DIR:-/opt/null-set-ai-dj/bin/versions}"
  local build="${DJC_BUILD:-./build-djcore.sh}" sha prod local_bin
  [[ -n "${DJ_CORE_SVC:-}" ]] && return 0
  [[ "${DJ_CORE_SKIP:-}" == "1" ]] && { echo "dj-core: skipped on request (DJ_CORE_SKIP=1) - moves that need the service will not run" >&2; return 0; }
  sha="$(sed -n 's/^sha=//p' "$pin" 2>/dev/null | head -n 1 | tr -d '[:space:]')"
  [[ -z "$sha" ]] && return 0                  # no pin file: the bridge's own default path applies
  prod="$vdir/$sha"
  [[ -x "$prod" ]] && return 0                 # the server install: the bridge finds it and checks its sha256
  if local_bin="$("$build" --path 2>/dev/null)" && [[ -n "$local_bin" ]]; then
    export DJ_CORE_SVC="$local_bin"; echo "dj-core: using the local build $local_bin"; return 0
  fi
  echo "dj-core: no service build for ${sha:0:9} ($prod missing) - building it once with $build" >&2
  if local_bin="$("$build")" && [[ -n "$local_bin" && -x "$local_bin" ]]; then
    export DJ_CORE_SVC="$local_bin"; echo "dj-core: using the local build $local_bin"; return 0
  fi
  echo "dj-core: the service is not available, so the console would run without the dj-core decisions." >&2
  echo "dj-core: fix the error above and run  $build  once, then ./start.sh again (or DJ_CORE_SKIP=1 ./start.sh to run without it)." >&2
  return 1
}
