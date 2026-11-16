# Running the web locally on a Mac: dj-core service and lyric remix plans

Two scripts at the repo root make the console work on a Mac the same way it does on the server.

## 1. The dj-core decision service: `./build-djcore.sh`

The console asks a small service (built from the private `dj-core` repo) for its decisions. The server installs the
build pinned in `deploy/dj-core-svc.version` under `/opt/null-set-ai-dj/bin/versions/<sha>`. On a Mac:

```bash
./build-djcore.sh          # builds the pinned sha once into .djcore/bin/<sha> (gitignored), prints the path
./start.sh                 # uses .djcore/bin/<sha> automatically
```

- Needs a Dart SDK: `dart` on `PATH` (`brew install dart-sdk`), else the one bundled with flutter, else `DART=/path/to/dart`.
- Finds the dj-core checkout in `DJ_CORE_DIR` (default `../dj-core`); clones `git@github.com:sanchitd5/dj-core.git` there when
  it is missing (needs your GitHub access). It fetches and builds exactly the pinned commit in a temporary worktree, so your
  working copy is never touched. Already built: it just prints the path. `./build-djcore.sh --path` / `--sha` build nothing.
- `start.sh` does nothing when `DJ_CORE_SVC` is set or the server's install path exists. Otherwise it uses the local build, builds
  it once when missing, and refuses to start (printing the command to run) when that fails, so the console never runs
  silently without the service. `DJ_CORE_SKIP=1 ./start.sh` runs without it on purpose.
- The pinned sha256 check applies to the server path only (`djcore_bridge`); a local build is a different binary.
- After pulling a web commit that changes `deploy/dj-core-svc.version`, the next `./start.sh` builds the new pin by itself.

## 2. Lyric remix plans: `./build-lyric-remix.sh`

Builds the lyric remix plans (a song's lines in a new order, or lines of two songs) from the Mac's library, judged by a model.
The builder is the private `dj-atlas` one (`git submodule update --init app/music_brain/knowledge`); the script reaches it only
through `app/music_brain/atlas_bridge.py` (`python -m app.music_brain.atlas_bridge lyric-remix`).

```bash
./build-lyric-remix.sh --dry-run                                   # counts only, no model, no writes
./start.sh --no-open &                                              # the local backend uses the omni server start.sh runs
./build-lyric-remix.sh --backend local --queue within --max-calls 200
./build-lyric-remix.sh --backend claudecode --queue pair --max-calls 100   # claude CLI logged in on this Mac, haiku class
```

| Option | |
|---|---|
| `--backend local` | the Qwen3-Omni server from `./start.sh` on `:$OMNI_PORT` (8901), or `OMNI_SERVER_URL` |
| `--backend claudecode` | the `claude` CLI logged in on the Mac (`--model` to change the model, `--fallback-local` to use the local model if it is unusable) |
| `--queue within\|pair\|both` | own lines of one song / lines of two songs / both. Run the two backends on different queues at the same time |
| `--max-calls N` | stop after N judge calls (builder default 40) |
| `--dry-run` | what a build would work with, and how many plans are already published |

Resumable: every judged candidate is kept in `data/cache/lyric_remix/state.json`; an interrupted or repeated run continues.
**Results** go to `app/music_brain/knowledge/lyric_remix.json.gz` (the dj-atlas checkout, `--out` changes it).

### Never judged twice (Mac and the CT116 night job)

Candidates have a deterministic id, so both sides ask about the same keys. Before a build the script marks every plan already in
the published file as judged, so the Mac does not ask about it again, and after the build it puts back published plans of songs
this library does not hold (same rules only), so Mac and server plans add up instead of replacing each other. Pull the latest
dj-atlas first (below) so "published" is current. Limits: rejected verdicts stay in each machine's own `state.json` (they are
not part of the published file), so a rejected candidate can be judged once more on the other side.

### Publish the Mac's plans to the server

No upload API exists or is needed; the existing path is git through dj-atlas:

```bash
cd app/music_brain/knowledge
git checkout main && git pull --ff-only origin main        # latest published plans first
cd ../../..
./build-lyric-remix.sh --max-calls 200                      # seeds from, and extends, the pulled file
cd app/music_brain/knowledge
git add lyric_remix.json.gz && git commit -m "lyric remix: Mac plans" && git push origin main
```

Then the lead bumps the submodule pointer in this repo (the server's `knowledge publish` / snapshot ships
`lyric_remix.json.gz` to the phone; CD never moves the submodule on prod by itself). The full
`python -m app.music_brain.matching.knowledge publish` also commits that file (it runs `git add -A` in the checkout) but
exports the whole cache as well; use it when the Mac's macros and atlas should go along.
