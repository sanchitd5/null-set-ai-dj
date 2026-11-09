#!/usr/bin/env python3
"""Release-notes history for the in-app "What's new" page (stdlib only; runs inside CT116 like apk_delta.py).

  changelog.py add  --dir D              append/refresh the entry for the build in D/null-set-dj.json
  changelog.py seed --dir D --file F     add the entries of F (a JSON list) whose version_code is not in the history yet

The history is D/changelog.json: a JSON list of {version_name, version_code, published_at, notes[, withdrawn]}, served
read-only by GET /api/app/changelog. `add` replaces the entry of the same version_code (a re-publish); `seed` never
overwrites. Older entries are never dropped.
"""
import argparse
import json
import os
import sys

NAME = "changelog.json"
KEYS = ("version_name", "version_code", "published_at", "notes")


def clean(e):
    """One entry reduced to the served keys, or None when it is not usable."""
    try:
        out = {"version_name": str(e["version_name"]), "version_code": int(e["version_code"]),
               "published_at": str(e["published_at"]), "notes": str(e["notes"])}
    except (KeyError, TypeError, ValueError):
        return None
    if out["version_code"] < 0 or isinstance(e["version_code"], bool):
        return None
    if e.get("withdrawn") is True:
        out["withdrawn"] = True
    return out


def load(d):
    try:
        data = json.load(open(os.path.join(d, NAME)))
    except (OSError, ValueError):
        return []
    return [c for c in map(clean, data if isinstance(data, list) else []) if c]


def save(d, entries):
    path = os.path.join(d, NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as t:   # plain open: CT116's app dir maps new files to nobody (see apk_delta.publish)
        json.dump(sorted(entries, key=lambda e: -e["version_code"]), t, indent=2)
    os.replace(tmp, path)


def add(d):
    meta = json.load(open(os.path.join(d, "null-set-dj.json")))
    new = clean(meta)
    if new is None:
        raise SystemExit("null-set-dj.json has no usable version/notes")
    old = {e["version_code"]: e for e in load(d)}
    if old.get(new["version_code"], {}).get("withdrawn"):
        new["withdrawn"] = True   # a re-publish does not un-withdraw
    old[new["version_code"]] = new
    save(d, list(old.values()))
    return len(old)


def seed(d, path):
    have = {e["version_code"]: e for e in load(d)}
    n = 0
    for e in json.load(open(path)):
        c = clean(e)
        if c and c["version_code"] not in have:
            have[c["version_code"]] = c
            n += 1
    save(d, list(have.values()))
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--dir", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--dir", required=True)
    s.add_argument("--file", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "add":
        print("changelog: %d entries" % add(args.dir))
    else:
        print("changelog: seeded %d new entries" % seed(args.dir, args.file))


if __name__ == "__main__":
    sys.exit(main())
