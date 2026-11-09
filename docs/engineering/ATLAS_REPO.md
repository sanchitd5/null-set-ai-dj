# dj-atlas: the knowledge folder as its own repo, which the server contributes to

`app/music_brain/knowledge/` (macros, learned techniques, genre / era labels, the slim pair atlas, track
names) moves out of this repo into `sanchitd5/dj-atlas` (private) and comes back as a **git submodule at
the same path**. Every path in the code (`knowledge.KNOWLEDGE_DIR`, the sync, the labels backfill) keeps
working. What changes: the server can commit and push what it learned, and the phone can pull the same
data without an APK.

Code: `app/music_brain/matching/knowledge_publish.py` (publish, update, version, snapshot, schedule),
`knowledge.py` (export, CLI), `knowledge_sync.py` (pull side, unchanged), `app/ui/services/knowledge_api.py`
(routes), `deploy/atlas/split_atlas.sh` (the one-time split). Tests: `app/tests/py/test_atlas_repo_*.py`,
`test_atlas_split_script.py`.

## Layout of dj-atlas (the folder as it is today)

```
names.json                     track id -> "Artist - Title"
genre_labels.json              track id -> {genre, era}
learned_techniques.json        technique kind -> {observations: [...]}
macros/<name>.json             one macro per file
atlas/meta.json                schema, rules (hash), built_at, tracks, shards, stats
atlas/pairs/<a>.json.gz        one gzip shard per track A: {"a>b": pair}
VERSION                        {"rules_hash": ..., "sha": <the commit this publish was made on top of>}
```

All JSON is sorted-key, stable and gzip has no timestamp, so identical knowledge is identical bytes and a
git diff only shows what changed. `VERSION` is bumped only by a publish that changes something (it cannot
hold its own commit sha, so it holds the parent's). The API's `sha` is the checkout's real HEAD.

## Flow

```
learn-set / maintain  -> cache (app.db)  -> knowledge export -> dj-atlas checkout (uncommitted)
                                              knowledge publish: fetch, merge by row, commit, push
dj-atlas main  -> knowledge update (submodule update --remote) -> knowledge sync -> cache
```

* **Phone leg**: after each set (and every 10 transitions, on launch, on resume/pause) the phone posts its
  on-disk outbox to `POST /api/mobile/learn` (`learn_ingest_api.py`, `learning/phone_learn.py`). Items have
  idempotent ids (`<set id>:<kind><n>`), a repeat is `duplicate`. Kinds: `set_log` (stored like
  `/api/set-logs`), `bad_pair` (owner veto, source `phone`), `like` (liked transition), `transition`
  (landed/skipped A -> B, a private `phone_pair` mark). Provenance (device, set) is a private `learn_item`
  mark, never exported. Transition evidence is overlaid onto the atlas pair's `played` (`played.phone`,
  works re-scored) right away, again before every publish (an atlas build drops it) and is idempotent.
  A batch holding a set log schedules the publish: one commit per set. Vetoes and likes reach the atlas
  at the next atlas build, as console ones do. Skipped picks count as one bad play, not a veto.
  No pair_atlas RULE_FILE is touched, so no rescore.
* **Pull (unchanged)**: `knowledge sync` / `auto_seed` read the checkout. Per-file version stamps are the
  newest git commit touching the file (inside the submodule, `git -C knowledge log` works as before).
* **Update**: `python -m app.music_brain.matching.knowledge update` runs
  `git submodule update --init --remote` for the checkout (a standalone clone: fetch + fast-forward), then
  sync. A dirty tree is stashed first and restored after; if the restore conflicts the stash stays and the
  command says so.
* **Publish**: `python -m app.music_brain.matching.knowledge publish [--no-push] [--dry-run]`.
  1. Fetch the remote branch (R). B = merge-base of the checkout's HEAD and R, the last state they shared.
     A dirty checkout is stashed (dropped again when an export reproduces it; kept if it held hand edits).
  2. The checkout moves to R and `sync` pulls R into the cache. Nothing is pulled from an empty remote.
  3. `export` rewrites the checkout from the cache (S).
  4. Merge B / S / R **per row** (see below), privacy scan, commit, push.
  5. Nothing changed after the merge: no commit, no VERSION bump, no push (`status: noop`).
  The commit subject carries the counts: `knowledge: server publish (3 macros, 41 pairs, 1 learned
  techniques, 6 labels, 2 names)`, the body adds pairs with new played evidence and the atlas rules hash.
  A push that loses a race (the remote moved) fetches and merges again, up to `DJ_ATLAS_ATTEMPTS` (3) times.
  A lock file in the checkout's git dir stops two publishes at once.
* **Background**: with `DJ_ATLAS_AUTOPUBLISH=1`, releasing the set lease (set ended) schedules a publish
  5 s later (runs coalesce), and a daemon thread publishes every `DJ_ATLAS_PUBLISH_HOURS` (24) h. Both call
  the same `publish`, push only with `DJ_ATLAS_PUSH=1`, and never raise into the server.

## Publish guard (no atlas regression)

A publish never makes the published atlas worse (2026-11-09: a server whose atlas build was older replaced a newer
one and dropped 18 tracks): (1) a server atlas on other rules replaces the published one only when its
`built_at` is newer, else the published atlas stays and the log says `guard: server atlas built_at ... is older`;
(2) `built_at` never moves backwards in a merge; (3) a track the published atlas holds and the server's atlas lacks
keeps its pair shard, its pairs as B in other shards and its meta row (`guard: kept N tracks ...`).

## Conflict rules (owner wins)

The row is the unit: a macro file, a learned technique, a label, one pair (`a>b`), a track in `meta.json`,
a name. For each row, with B, S (server) and R (remote):

| situation | result |
|---|---|
| S = R | as is |
| only S changed since B | the server's row is published |
| only R changed (owner or another machine) | R's row stays |
| both changed | **R's row wins**; a pair keeps the server's `played` evidence when R's row has none |
| R deleted a row, S changed it | stays deleted |
| R's row was kept in an earlier publish and has not changed since | stays R's (macros / learned only until `knowledge take ITEM --local`; pairs / labels until the owner edits the row again) |
| R's atlas is on other `rules` than B's | the whole `atlas/` folder stays R's (pairs from other rules are never mixed) |
| only the server's atlas moved to new rules | the server's atlas replaces the published one |

This is the mirror of `knowledge_sync`, where the local row wins over a published change. Between them the
owner's manual edit survives in both directions. `knowledge conflicts` / `take ITEM --published|--local`
work as before. The first publish from a checkout with no shared history lets R win on every row both sides
have, and publishes only rows R lacks.

## What never leaves the machine

Before any commit, every changed file is decoded and scanned: `knowledge.privacy_hits` (absolute paths,
the home dir, e-mail addresses) plus the repo's `test_no_personal_paths` rule (`/Users/<name>/`,
`/home/<name>/`, placeholders `me`, `x`, `someone` allowed). A hit stops the publish with
`PublishError`, nothing is committed. `export` itself refuses private strings the same way. No keys are in
code or in the repo: the deploy key is a file path in the environment.

## API for the phone

Behind the normal auth (device token or session), like every `/api` route.

* `GET /api/knowledge/version` -> `{sha, rules_hash, files, bytes, dirty}`. `sha` is the checkout's HEAD,
  `rules_hash` the atlas rules in `meta.json`, `files` / `bytes` what the snapshot holds. Cheap, poll it.
* `GET /api/knowledge/snapshot` -> `application/zip` in the layout of the phone's bundled `assets/atlas`:
  `meta.json`, `names.json`, `genre_labels.json`, `learned_techniques.json`, `macros.json`
  (`{"macros": [...]}`), `pairs/<a>.json.gz`, plus `version.json`. `ETag` is `"<sha>"` (`"<sha>+<hash>"` while
  the checkout has uncommitted changes); `If-None-Match` answers `304` with no body. Deterministic bytes.
  The phone's other bundled files (`seed_combos.json`, `studied_sets.json`, `vetoes_seed.json`) are not
  knowledge and stay in the APK.

ATLAS2 (phone): check `version` on start and every N hours, metered-aware; on a changed `sha` fetch the
snapshot with `If-None-Match`, unzip, feed the existing snapshot import (the `shards` list in `meta.json`
names the `pairs/` entries), keep the bundled asset as the offline fallback.

## Environment

| variable | meaning |
|---|---|
| `DJ_ATLAS_PUSH=1` | publish pushes; without it publish only commits locally |
| `DJ_ATLAS_REMOTE_URL` | URL of dj-atlas; sets the checkout's `origin` (not needed when the submodule already points there) |
| `DJ_ATLAS_SSH_KEY` | path of the deploy key file, used through `GIT_SSH_COMMAND` (`IdentitiesOnly`, `BatchMode`) |
| `DJ_ATLAS_BRANCH` | branch (default `main`) |
| `DJ_ATLAS_AUTOPUBLISH=1` | arms the set-end hook and the daily thread |
| `DJ_ATLAS_PUBLISH_HOURS` | period of the daily thread (default 24, minimum 0.25) |
| `DJ_ATLAS_ATTEMPTS` | merge-and-push tries when the push races (default 3) |
| `DJ_ATLAS_AUTHOR_NAME` / `_EMAIL` | commit identity (default `dj-atlas-bot`) |

State the publish keeps: `CACHE_DIR/knowledge_publish.json` (rows left as the remote had them).

## One-time steps (owner / lead)

1. **Create the repo**: private `sanchitd5/dj-atlas`, empty (no README, no licence).
2. **Run the split** (lead, from a clean checkout of this branch merged to main):
   `deploy/atlas/split_atlas.sh --dry-run` first (scratch dir only), then
   `deploy/atlas/split_atlas.sh --remote-url git@github.com:sanchitd5/dj-atlas.git`. It uses `git filter-repo`
   when installed, else `git subtree split`; checks commit count, subjects and that the new root tree equals
   `app/music_brain/knowledge` at HEAD; pushes the history to dj-atlas; replaces the folder with a submodule at
   the same path and commits that. Then push this repo yourself.
3. **Deploy key**: `ssh-keygen -t ed25519 -N '' -f dj-atlas-deploy` on the server, add the `.pub` to dj-atlas
   under Settings -> Deploy keys with **write access**, keep the private key outside the repo
   (e.g. `~/.ssh/dj-atlas-deploy`, mode 600).
4. **Prod environment**: `DJ_ATLAS_SSH_KEY=<that path>`, `DJ_ATLAS_PUSH=1`, `DJ_ATLAS_AUTOPUBLISH=1`; deploy
   with `git submodule update --init` (and `git clone --recurse-submodules` for new checkouts). Set the
   submodule URL to the SSH form on prod (`git submodule set-url` / `.git/config`) if the HTTPS URL has no
   credentials there.
5. **CI**: the tests use a temp knowledge dir, so CI needs no submodule. Add `submodules: true` and a token
   to `actions/checkout` only if a job reads the real folder.
6. Phone leg needs nothing extra: it rides on the same env vars (step 4) and the APK that carries the outbox.
   Without `DJ_ATLAS_AUTOPUBLISH=1` the server still ingests and overlays; only the commit is skipped.
7. Check: `python -m app.music_brain.matching.knowledge publish --dry-run`, then a real `publish`.

## Not done here

* The real split, the GitHub repo, pushes to dj-atlas and any prod change are left to the lead.
* After the split, `test_no_personal_paths` no longer sees the knowledge files (they are a gitlink); the
  publish scan above is what covers them.
* ATLAS2 (the phone side) is separate.
