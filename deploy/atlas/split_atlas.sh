#!/usr/bin/env bash
# Split app/music_brain/knowledge/ out of this repo into its own repo (sanchitd5/dj-atlas), with the
# folder's full history, and replace the folder here with a git submodule at the same path.
#
#   deploy/atlas/split_atlas.sh --dry-run            prove history + layout in a scratch dir, touch nothing
#   deploy/atlas/split_atlas.sh                      the real split (owner / lead only, see docs/engineering/ATLAS_REPO.md)
#
# Options:
#   --path P         folder to extract (default app/music_brain/knowledge)
#   --remote-url U   where dj-atlas lives (default https://github.com/sanchitd5/dj-atlas.git)
#   --repo R         the repo to split (default: the repo this script sits in)
#   --branch B       branch name in the new repo (default main)
#   --work D         scratch dir (default: a mktemp -d, removed on exit; with --work it is kept)
#   --dry-run        extract into scratch, verify, simulate the submodule swap in a scratch clone, push nothing
#
# Extraction: `git filter-repo --subdirectory-filter` when installed, else `git subtree split`.
# Checks (both modes): same commit subjects in the same order as the folder's history here, the new
# repo's root tree is byte-identical to the folder's tree at HEAD (so the submodule pin yields exactly
# today's files), nothing outside the folder came along, and (dry run) the swapped clone has a
# 160000 gitlink at the path pinned to the new repo's tip.
#
# The real run: pushes the split history to --remote-url (the repo must exist and be empty; this
# script never creates GitHub repos), then in --repo removes the folder, `git submodule add`s the new
# repo at the same path and commits that on the current branch. It does not push --repo.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATH_IN_REPO="app/music_brain/knowledge"
REMOTE_URL="https://github.com/sanchitd5/dj-atlas.git"
REPO="$(git -C "$SELF_DIR" rev-parse --show-toplevel)"
BRANCH="main"
WORK=""
KEEP=0
DRY=0

die() { echo "split_atlas: $*" >&2; exit 1; }
say() { echo "split_atlas: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --path) PATH_IN_REPO="${2:?--path needs a value}"; shift ;;
    --remote-url) REMOTE_URL="${2:?--remote-url needs a value}"; shift ;;
    --repo) REPO="$(cd "${2:?--repo needs a value}" && git rev-parse --show-toplevel)"; shift ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift ;;
    --work) WORK="${2:?--work needs a value}"; KEEP=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

PATH_IN_REPO="${PATH_IN_REPO%/}"
case "$PATH_IN_REPO" in ""|/*|*..*) die "--path must be a relative folder inside the repo" ;; esac
git -C "$REPO" rev-parse --verify -q HEAD >/dev/null || die "$REPO has no commits"
[ -d "$REPO/$PATH_IN_REPO" ] || die "$PATH_IN_REPO is not a folder in $REPO"
if [ -n "$(git -C "$REPO" ls-tree HEAD -- "$PATH_IN_REPO" | grep '^160000' || true)" ]; then
  die "$PATH_IN_REPO is already a submodule"
fi

if [ -z "$WORK" ]; then
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
else
  mkdir -p "$WORK"
  [ -z "$(ls -A "$WORK")" ] || die "--work $WORK is not empty"
fi
WORK="$(cd "$WORK" && pwd)"

# ---------------------------------------------------------------------------------- extract
# Always from a throw-away clone: the real repo's refs and worktree are never touched by the extraction.
git clone -q --no-local "$REPO" "$WORK/src"
git -C "$WORK/src" checkout -q --detach "$(git -C "$REPO" rev-parse HEAD)"

if git filter-repo --version >/dev/null 2>&1; then
  METHOD="git filter-repo"
  git -C "$WORK/src" filter-repo --quiet --force --subdirectory-filter "$PATH_IN_REPO"
  git -C "$WORK/src" branch -q -f "$BRANCH" HEAD
  git init -q -b "$BRANCH" "$WORK/atlas"
  git -C "$WORK/atlas" fetch -q "$WORK/src" "refs/heads/$BRANCH"
else
  METHOD="git subtree split"
  git -C "$WORK/src" subtree split -q --prefix="$PATH_IN_REPO" -b atlas-split HEAD >/dev/null
  git init -q -b "$BRANCH" "$WORK/atlas"
  git -C "$WORK/atlas" fetch -q "$WORK/src" "refs/heads/atlas-split"
fi
git -C "$WORK/atlas" checkout -q -B "$BRANCH" FETCH_HEAD
say "extracted with $METHOD"

# ---------------------------------------------------------------------------------- verify
src_n="$(git -C "$REPO" rev-list --count --no-merges HEAD -- "$PATH_IN_REPO")"
new_n="$(git -C "$WORK/atlas" rev-list --count --no-merges "$BRANCH")"
[ "$src_n" = "$new_n" ] || die "history mismatch: $src_n commits touch $PATH_IN_REPO here, the split has $new_n"

want="$(git -C "$REPO" log --no-merges --format='%s' HEAD -- "$PATH_IN_REPO")"
got="$(git -C "$WORK/atlas" log --no-merges --format='%s' "$BRANCH")"
[ "$want" = "$got" ] || die "commit subjects differ between the folder's history and the split"

src_tree="$(git -C "$REPO" rev-parse "HEAD:$PATH_IN_REPO")"
new_tree="$(git -C "$WORK/atlas" rev-parse "$BRANCH^{tree}")"
[ "$src_tree" = "$new_tree" ] || die "tree mismatch: $PATH_IN_REPO@HEAD is $src_tree, the split's root is $new_tree"

if git -C "$WORK/atlas" ls-tree -r --name-only "$BRANCH" | grep -q '^app/'; then
  die "the split still carries paths outside the folder (app/...)"
fi
n_files="$(git -C "$WORK/atlas" ls-tree -r --name-only "$BRANCH" | wc -l | tr -d ' ')"
tip="$(git -C "$WORK/atlas" rev-parse "$BRANCH")"
say "verified: $new_n commits, $n_files files, root tree == $PATH_IN_REPO@HEAD ($src_tree), tip $tip"
git -C "$WORK/atlas" ls-tree --name-only "$BRANCH" | sed 's/^/  layout: /'

# ---------------------------------------------------------------------------------- swap
# Dry run: the swap happens in a scratch clone and the "remote" is the scratch repo. Real run: in $REPO.
swap() {  # swap <worktree> <url> <git -c options...>
  local wt="$1" url="$2"; shift 2
  git -C "$wt" "$@" rm -r -q "$PATH_IN_REPO"
  rm -rf "${wt:?}/$PATH_IN_REPO"
  git -C "$wt" "$@" submodule add -q -b "$BRANCH" "$url" "$PATH_IN_REPO"
  git -C "$wt" "$@" commit -q -m "knowledge: app/music_brain/knowledge is now the dj-atlas submodule

The folder's history lives on in the dj-atlas repo; this commit pins its tip ($tip)."
}

if [ "$DRY" = 1 ]; then
  git clone -q --no-local "$REPO" "$WORK/sim"
  git -C "$WORK/sim" checkout -q --detach "$(git -C "$REPO" rev-parse HEAD)"
  git clone -q --bare "$WORK/atlas" "$WORK/atlas.git"
  swap "$WORK/sim" "$WORK/atlas.git" -c user.name=split-dry-run -c user.email=split@invalid.invalid -c protocol.file.allow=always
  mode_sha="$(git -C "$WORK/sim" ls-tree HEAD -- "$PATH_IN_REPO" | awk '{print $1" "$3}')"
  [ "$mode_sha" = "160000 $tip" ] || die "gitlink wrong: got '$mode_sha', want '160000 $tip'"
  grep -q "path = $PATH_IN_REPO" "$WORK/sim/.gitmodules" || die ".gitmodules lacks the path"
  # the checked-out submodule holds exactly the files the folder holds today
  if ! diff -r -q --exclude=.git "$REPO/$PATH_IN_REPO" "$WORK/sim/$PATH_IN_REPO" >/dev/null; then
    die "submodule worktree differs from the folder it replaces"
  fi
  say "dry run OK: $PATH_IN_REPO becomes a 160000 gitlink pinned to $tip; .gitmodules:"
  sed 's/^/  /' "$WORK/sim/.gitmodules"
  say "nothing pushed, nothing in $REPO touched"
  exit 0
fi

# ---------------------------------------------------------------------------------- real run
[ -z "$(git -C "$REPO" status --porcelain)" ] || die "the working tree of $REPO is not clean; commit or stash first"
git -C "$REPO" symbolic-ref -q HEAD >/dev/null || die "$REPO is on a detached HEAD; check out a branch first"
say "pushing the split to $REMOTE_URL ($BRANCH)"
git -C "$WORK/atlas" remote add origin "$REMOTE_URL"
git -C "$WORK/atlas" push origin "$BRANCH:$BRANCH"
swap "$REPO" "$REMOTE_URL"
say "done: $PATH_IN_REPO is now a submodule of $REMOTE_URL pinned to $tip. Review, then push $REPO yourself."
