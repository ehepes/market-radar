#!/bin/bash
#
# Push changed files to GitHub as ONE incremental commit.
#
# Unlike deploy.sh (which builds a complete tree and is for first upload only),
# this layers a commit on top of what is already there — so files the workflow
# itself writes (docs/data.json, state/radar.db) are preserved.
#
#   GH_TOKEN=ghp_xxx bash push.sh radar/signals.py config/rules.toml
#   GH_TOKEN=ghp_xxx MESSAGE="fix drawdown semantics" bash push.sh <files...>
#
set -euo pipefail

API="https://api.github.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mFailed:\033[0m %s\n' "$1" >&2; exit 1; }

[ $# -gt 0 ] || die "name at least one file to push"
GH_TOKEN="$(printf '%s' "${GH_TOKEN:-}" | tr -d '[:space:]')"
[ -n "$GH_TOKEN" ] || die "set GH_TOKEN=..."
REPO_NAME="${REPO_NAME:-market-radar}"
MESSAGE="${MESSAGE:-Update $# file(s)}"

HTTP_BODY=""; HTTP_CODE=""
call() {
  local method="$1" path="$2" body_file="${3:-}" tmp
  tmp="$(mktemp)"
  local args=(-sS -o "$tmp" -w '%{http_code}' -X "$method"
    -H "Authorization: Bearer ${GH_TOKEN}"
    -H "Accept: application/vnd.github+json"
    -H "X-GitHub-Api-Version: 2022-11-28")
  [ -n "$body_file" ] && args+=(-H "Content-Type: application/json" --data-binary @"$body_file")
  HTTP_CODE="$(curl "${args[@]}" "${API}${path}")"
  HTTP_BODY="$(cat "$tmp")"; rm -f "$tmp"
}
err() { echo "$HTTP_BODY" | jq -r '.message // "unknown"'; }

call GET /user
[ "$HTTP_CODE" = "200" ] || die "token rejected (HTTP $HTTP_CODE): $(err)"
USER="$(echo "$HTTP_BODY" | jq -r .login)"
R="/repos/${USER}/${REPO_NAME}"
ok "authenticated as $USER"

call GET "$R"
[ "$HTTP_CODE" = "200" ] || die "cannot read $USER/$REPO_NAME: $(err)"
BRANCH="$(echo "$HTTP_BODY" | jq -r '.default_branch // "main"')"

call GET "$R/git/ref/heads/${BRANCH}"
[ "$HTTP_CODE" = "200" ] || die "branch '$BRANCH' has no commits — use deploy.sh first"
HEAD_SHA="$(echo "$HTTP_BODY" | jq -r '.object.sha')"
call GET "$R/git/commits/${HEAD_SHA}"
BASE_TREE="$(echo "$HTTP_BODY" | jq -r '.tree.sha')"
ok "building on ${HEAD_SHA:0:7}"

ITEMS="$(mktemp)"; : > "$ITEMS"
for f in "$@"; do
  [ -f "$f" ] || die "no such file: $f"
  f="${f#./}"
  BLOB="$(mktemp)"
  jq -n --arg c "$(base64 < "$f" | tr -d '\n')" '{content:$c, encoding:"base64"}' > "$BLOB"
  call POST "$R/git/blobs" "$BLOB"; rm -f "$BLOB"
  [ "$HTTP_CODE" = "201" ] || die "blob for '$f' failed (HTTP $HTTP_CODE): $(err)"
  MODE="100644"; case "$f" in *.sh) MODE="100755" ;; esac
  jq -n --arg p "$f" --arg s "$(echo "$HTTP_BODY" | jq -r .sha)" --arg m "$MODE" \
    '{path:$p, mode:$m, type:"blob", sha:$s}' >> "$ITEMS"
  ok "staged $f"
done

TB="$(mktemp)"
# base_tree is what makes this incremental: everything not listed is inherited.
jq -s --arg base "$BASE_TREE" '{base_tree:$base, tree:.}' "$ITEMS" > "$TB"; rm -f "$ITEMS"
call POST "$R/git/trees" "$TB"; rm -f "$TB"
[ "$HTTP_CODE" = "201" ] || die "tree failed (HTTP $HTTP_CODE): $(err)"
TREE="$(echo "$HTTP_BODY" | jq -r .sha)"

CB="$(mktemp)"
jq -n --arg t "$TREE" --arg p "$HEAD_SHA" --arg m "$MESSAGE" \
  '{message:$m, tree:$t, parents:[$p]}' > "$CB"
call POST "$R/git/commits" "$CB"; rm -f "$CB"
[ "$HTTP_CODE" = "201" ] || die "commit failed (HTTP $HTTP_CODE): $(err)"
COMMIT="$(echo "$HTTP_BODY" | jq -r .sha)"

RB="$(mktemp)"
jq -n --arg s "$COMMIT" '{sha:$s}' > "$RB"
call PATCH "$R/git/refs/heads/${BRANCH}" "$RB"; rm -f "$RB"
[ "$HTTP_CODE" = "200" ] || die "ref update failed (HTTP $HTTP_CODE): $(err)"

GH_TOKEN=""
ok "pushed ${COMMIT:0:7} to ${BRANCH}"
echo
echo "  https://github.com/${USER}/${REPO_NAME}/commit/${COMMIT}"
