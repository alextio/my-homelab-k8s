#!/usr/bin/env bash
# Render a base ref and the working tree, and print the manifest diff.
# Both sides are rendered with this checkout's ci/render.py, so a change to
# the renderer itself doesn't show up as a manifest diff.
#
# Usage: ci/diff.sh [base-ref]   (default: origin/main)
set -euo pipefail
base=${1:-origin/main}
root=$(git rev-parse --show-toplevel)
work=$(mktemp -d)
trap 'git -C "$root" worktree remove --force "$work/base-src" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

git -C "$root" worktree add -q --detach "$work/base-src" "$base"
render() {  # render <src> <out>; quiet unless it fails
  python3 "$root/ci/render.py" --repo-root "$1" --out "$2" >"$work/log" 2>&1 \
    || { echo "rendering $1 failed:" >&2; cat "$work/log" >&2; exit 1; }
}
render "$work/base-src" "$work/base"
render "$root" "$work/head"

cd "$work"
diff -ruN base head || true
