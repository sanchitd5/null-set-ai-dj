# Changelog

All notable changes to NULL::SET. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-09-30

### Added
- **Punjabi scene profile.** A `PUNJABI` setting in the toggle drawer (`auto`, `on`, `off`, default `auto`).
  When both songs are Punjabi it treats punjabi, bhangra and desi as one scene (Bollywood as a neighbour),
  widens the era gate to 4 decades, plays 45 to 90 s snippets, folds 88 and 176 BPM as the same feel and
  falls back to a Quick Cut on the downbeat instead of an Echo Out. When only one side is Punjabi it
  changes only the handover. The step log says when the profile is active and why. With the profile
  `off`, every decision is byte-identical to before (golden-tested). Profile values come from the research
  notes and are marked as guesses until tested by ear.
- **Scene-tagged learned moves.** Moves learned from a set tagged Punjabi count toward that scene only, so
  they never loosen the global rules. Under the full Punjabi profile, a learned stem intro seen on a key
  clash in at least 3 Punjabi sightings may blend on a clash. The 8 % key-lock stretch cap still holds: a
  bigger gap becomes a Quick Cut.
- **`learn-set` writes macros.** After a successful learn it imports the set's songs and rebuilds the pair
  atlas incrementally, writing `studied-<set_id>-<n>` per transition and `studied-set-<set_id>` for the
  whole set. The result reports them under `macros`. `--no-macros` skips the step.
- **`learn-set --macros-only`.** Builds a playable `set-<set_id>` macro from a tracklist alone: no set
  download, no stem separation of the mix.
- **Tracked knowledge (`app/music_brain/knowledge/`).** Every macro, the learned observations and a slim
  pair atlas are exported to the repo, so a fresh checkout starts with the player's knowledge. Songs are
  matched by name on another machine, and the local cache always wins. `learn-set` exports at the end;
  `python -m app.music_brain.knowledge export` does it by hand.
- **Newly studied sets:** Argy at Tomorrowland Brasil 2025 and DJ Timeless "NYC Live Sessions 1", with their
  macros. More studied-set songs are now in the library, so the published atlas covers 492 tracks.
- **CI.** GitHub Actions runs the console JS checks and the pytest suite (not slow) on every push and pull
  request, on Python 3.14 and Node 24, from `requirements-ci.txt`.
- **`ANNEX.md`**, a glossary of every named concept (atlas, macros, supermove, merge-hold, recipes, gates,
  scene profiles, the sim), each tied to the code that defines it.
- `--punjabi auto|on|off` for the virtual set sim and its suite.

### Changed
- **The pair atlas is a segmented folder** (`pair_atlas/meta.json`, `tracks/<id>.json`, `pairs/<a>.json`).
  A build that changes nothing writes nothing, a request reads only the shards it needs, and the
  published copy is split the same way. The old single file is migrated once and kept as
  `pair_atlas.json.migrated`.
- Exports no longer rewrite a macro whose only change is its `created` timestamp.
- The repository is named `null-set-ai-dj` throughout the docs; machine-specific paths from an earlier
  origin were removed.

### Fixed
- Two `learn-set` runs finishing together could drop each other's observations: the learned store is now
  locked across load and save, and the atlas build is locked too.
- Concurrent writers no longer share one temp file (per-process temp names).
- `learn-set` reported `written: []` for a set id with capital letters although its macros were saved.

### Removed
- 717 lines of unused code in `app/legacy_pipeline/` (unreachable functions, rule tables and a
  commented-out block). The six documented legacy entry points are unchanged.
- The studied "Desi Beats on Mumbai Streets" rickshaw set and its 16 observations.

## [1.0.0] - 2026-09-29

First public release (tag `v1`): two-deck autopilot with merge, hold and transition on 4 stems, learned
moves from studied DJ sets, the pair atlas, macros, NULL-BOT and the visuals, the virtual set sim and the
Obsidian DJ knowledge base. See the [v1 release notes](https://github.com/sanchitd5/null-set-ai-dj/releases/tag/v1).

[1.0.1]: https://github.com/sanchitd5/null-set-ai-dj/compare/v1...v1.0.1
[1.0.0]: https://github.com/sanchitd5/null-set-ai-dj/releases/tag/v1
