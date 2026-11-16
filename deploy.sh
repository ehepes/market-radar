#!/bin/bash
#
# Market Radar — one-shot deployment to GitHub.
#
# Creates the repository, uploads every file as a single commit, turns on the
# Pages dashboard, and prints exactly what is left for you to do by hand.
#
# Needs only curl, jq and base64 — all already on macOS. No git required,
# which matters because this Mac has no working git.
#
# Your token is read with `read -s`: it is never echoed, never written to disk,
# and never leaves this terminal.
#
#   bash deploy.sh
#
set -euo pipefail

API="https://api.github.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mFailed:\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

for tool in curl jq base64; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not installed."
done

bold "Market Radar — deploy to GitHub"
echo
echo "You need a GitHub personal access token with TWO scopes:"
echo "    repo        (create the repository and push files)"
echo "    workflow    (push the scheduled-run workflow file)"
echo
echo "Create one here, choosing 'Tokens (classic)':"
echo "    https://github.com/settings/tokens/new?scopes=repo,workflow&description=Market%20Radar%20deploy"
echo
echo "The token is hidden as you paste it. Nothing is saved to disk."
echo

# Every answer can arrive as an environment variable instead of a prompt, so
# this works in terminals that do not forward keystrokes to a running process:
#
#   GH_TOKEN=ghp_xxx bash deploy.sh
#   GH_TOKEN_FILE=~/gh-token.txt bash deploy.sh   (keeps it out of shell history)
#
# Optional: REPO_NAME (default market-radar), REPO_PRIVATE=true (default public),
# GH_USER (not needed — the username is read from the token).

# The token already identifies its owner, so there is nothing to ask for and
# nothing to get wrong. GH_USER, if given, is only used to warn about a
# mismatch; the token's own login always wins.
GH_USER="$(printf '%s' "${GH_USER:-}" | tr -d '[:space:]')"

GH_TOKEN="${GH_TOKEN:-}"
if [ -z "$GH_TOKEN" ] && [ -n "${GH_TOKEN_FILE:-}" ]; then
  [ -f "$GH_TOKEN_FILE" ] || die "GH_TOKEN_FILE '$GH_TOKEN_FILE' does not exist"
  GH_TOKEN="$(cat "$GH_TOKEN_FILE")"
fi
if [ -z "$GH_TOKEN" ]; then
  read -r -s -p "Paste token (hidden, nothing will appear): " GH_TOKEN || \
    die "could not read input — set GH_TOKEN or GH_TOKEN_FILE instead"
  echo
fi
# A pasted token often carries a trailing newline or stray space; both would
# produce a confusing 401.
GH_TOKEN="$(printf '%s' "$GH_TOKEN" | tr -d '[:space:]')"
[ -n "$GH_TOKEN" ] || die "token is required (or set GH_TOKEN=... / GH_TOKEN_FILE=...)"

REPO="${REPO_NAME:-}"
if [ -z "$REPO" ]; then
  read -r -p "Repository name [market-radar]: " REPO || REPO=""
fi
REPO="$(printf '%s' "${REPO:-market-radar}" | tr -d '[:space:]')"

if [ -n "${REPO_PRIVATE:-}" ]; then
  case "$REPO_PRIVATE" in [Tt]rue|1|[Yy]es) PRIVATE=true ;; *) PRIVATE=false ;; esac
else
  echo
  echo "GitHub's free plan only serves Pages from a PUBLIC repo."
  echo "Public  = phone dashboard works. Your positions stay private anyway:"
  echo "          they live in an encrypted secret and are withheld from the page."
  echo "Private = no dashboard, alerts only."
  read -r -p "Make the repository public? [Y/n]: " VIS_ANS || VIS_ANS="y"
  case "${VIS_ANS:-y}" in [Nn]*) PRIVATE=true ;; *) PRIVATE=false ;; esac
fi

# --------------------------------------------------------------- http layer

HTTP_BODY=""
HTTP_CODE=""

call() { # method path [body-file]
  local method="$1" path="$2" body_file="${3:-}" tmp
  tmp="$(mktemp)"
  local args=(-sS -o "$tmp" -w '%{http_code}' -X "$method"
    -H "Authorization: Bearer ${GH_TOKEN}"
    -H "Accept: application/vnd.github+json"
    -H "X-GitHub-Api-Version: 2022-11-28"
    -H "User-Agent: market-radar-deploy")
  [ -n "$body_file" ] && args+=(-H "Content-Type: application/json" --data-binary @"$body_file")
  HTTP_CODE="$(curl "${args[@]}" "${API}${path}")"
  HTTP_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

api_error() { echo "$HTTP_BODY" | jq -r '.message // "unknown error"' 2>/dev/null || echo "unparseable response"; }

# ------------------------------------------------------------ authenticate

echo
bold "1. Checking the token"
call GET /user
[ "$HTTP_CODE" = "200" ] || die "token rejected (HTTP $HTTP_CODE): $(api_error)"
LOGIN="$(echo "$HTTP_BODY" | jq -r '.login')"
[ -n "$LOGIN" ] && [ "$LOGIN" != "null" ] || die "GitHub did not return a username for this token"
if [ -z "$GH_USER" ]; then
  ok "authenticated as $LOGIN (username read from the token)"
elif [ "$LOGIN" != "$GH_USER" ]; then
  warn "token belongs to '$LOGIN', not '$GH_USER' — using '$LOGIN'"
else
  ok "authenticated as $LOGIN"
fi
GH_USER="$LOGIN"

# The workflow scope is not visible on the token object, so verify it the only
# way that is reliable: by pushing and reading the error. Warn early instead.
SCOPES="$(curl -sS -I -H "Authorization: Bearer ${GH_TOKEN}" "${API}/user" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="x-oauth-scopes"{print $2}')"
if [ -n "$SCOPES" ]; then
  case "$SCOPES" in
    *workflow*) ok "scopes: $SCOPES" ;;
    *) warn "scopes: ${SCOPES:-none} — 'workflow' is missing, the push will fail"
       read -r -p "  Continue anyway? [y/N]: " c; case "${c:-n}" in [Yy]*) ;; *) die "add the workflow scope and re-run";; esac ;;
  esac
fi

# -------------------------------------------------------------- create repo

echo
bold "2. Creating the repository"
REPO_BODY="$(mktemp)"
jq -n --arg name "$REPO" --argjson private "$PRIVATE" \
  '{name:$name, private:$private, auto_init:false,
    description:"Rule-based market monitoring with Revolut-Ireland instrument mapping",
    has_issues:false, has_wiki:false, has_projects:false}' > "$REPO_BODY"
call POST /user/repos "$REPO_BODY"
rm -f "$REPO_BODY"

case "$HTTP_CODE" in
  201) ok "created ${GH_USER}/${REPO} ($([ "$PRIVATE" = true ] && echo private || echo public))" ;;
  422) warn "${GH_USER}/${REPO} already exists — uploading into it"
       call GET "/repos/${GH_USER}/${REPO}"
       [ "$HTTP_CODE" = "200" ] || die "cannot access the existing repo: $(api_error)"
       PRIVATE="$(echo "$HTTP_BODY" | jq -r '.private')" ;;
  *)   die "could not create the repo (HTTP $HTTP_CODE): $(api_error)" ;;
esac

# Respect the account's default-branch setting rather than assuming "main".
call GET "/repos/${GH_USER}/${REPO}"
BRANCH="$(echo "$HTTP_BODY" | jq -r '.default_branch // empty')"
[ -n "$BRANCH" ] || BRANCH="main"
ok "target branch: $BRANCH"

# A repository with zero commits rejects the git-data API ("Git Repository is
# empty", HTTP 409): blobs and trees need an existing commit to hang off. The
# Contents API does work on an empty repo, so seed one real file with it and
# the rest of this script proceeds normally. The seed is README.md, so a run
# that fails later still leaves something coherent behind — and because the
# tree built below is complete rather than incremental, this commit is fully
# superseded rather than layered on top.
call GET "/repos/${GH_USER}/${REPO}/git/ref/heads/${BRANCH}"
if [ "$HTTP_CODE" != "200" ]; then
  warn "repository has no commits yet — seeding an initial one"
  SEED_BODY="$(mktemp)"
  jq -n --arg b "$BRANCH" --arg c "$(base64 < README.md | tr -d '\n')" \
    '{message:"Initial commit", content:$c, branch:$b}' > "$SEED_BODY"
  call PUT "/repos/${GH_USER}/${REPO}/contents/README.md" "$SEED_BODY"
  rm -f "$SEED_BODY"
  case "$HTTP_CODE" in
    201|200) ok "initial commit created" ;;
    *) die "could not seed the empty repository (HTTP $HTTP_CODE): $(api_error)" ;;
  esac
fi

# ------------------------------------------------------------- upload files

echo
bold "3. Uploading files"

FILES="$(find . -type f \
  ! -path './.git/*' \
  ! -name '.DS_Store' \
  ! -path '*/__pycache__/*' \
  ! -name '*.pyc' \
  ! -path './state/radar.db' \
  | sed 's|^\./||' | sort)"
TOTAL="$(echo "$FILES" | wc -l | tr -d ' ')"
echo "  $TOTAL files"

TREE_ITEMS="$(mktemp)"
: > "$TREE_ITEMS"
N=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  N=$((N + 1))
  BLOB="$(mktemp)"
  jq -n --arg c "$(base64 < "$f" | tr -d '\n')" '{content:$c, encoding:"base64"}' > "$BLOB"
  call POST "/repos/${GH_USER}/${REPO}/git/blobs" "$BLOB"
  rm -f "$BLOB"
  [ "$HTTP_CODE" = "201" ] || die "uploading '$f' failed (HTTP $HTTP_CODE): $(api_error)"
  SHA="$(echo "$HTTP_BODY" | jq -r '.sha')"
  # Keep the executable bit on shell scripts; everything else is a plain file.
  MODE="100644"; case "$f" in *.sh) MODE="100755" ;; esac
  jq -n --arg path "$f" --arg sha "$SHA" --arg mode "$MODE" \
    '{path:$path, mode:$mode, type:"blob", sha:$sha}' >> "$TREE_ITEMS"
  printf '\r  uploaded %d/%d' "$N" "$TOTAL"
done <<< "$FILES"
printf '\n'
ok "all blobs uploaded"

TREE_BODY="$(mktemp)"
jq -s '{tree: .}' "$TREE_ITEMS" > "$TREE_BODY"
rm -f "$TREE_ITEMS"
call POST "/repos/${GH_USER}/${REPO}/git/trees" "$TREE_BODY"
rm -f "$TREE_BODY"
[ "$HTTP_CODE" = "201" ] || die "building the tree failed (HTTP $HTTP_CODE): $(api_error)"
TREE_SHA="$(echo "$HTTP_BODY" | jq -r '.sha')"
ok "tree built"

# Is there an existing commit to parent onto?
call GET "/repos/${GH_USER}/${REPO}/git/ref/heads/${BRANCH}"
PARENTS='[]'
if [ "$HTTP_CODE" = "200" ]; then
  PARENT_SHA="$(echo "$HTTP_BODY" | jq -r '.object.sha')"
  PARENTS="$(jq -n --arg s "$PARENT_SHA" '[$s]')"
  REF_EXISTS=true
else
  REF_EXISTS=false
fi

COMMIT_BODY="$(mktemp)"
jq -n --arg tree "$TREE_SHA" --argjson parents "$PARENTS" \
  --arg msg "Market Radar: rule-based monitoring engine" \
  '{message:$msg, tree:$tree, parents:$parents}' > "$COMMIT_BODY"
call POST "/repos/${GH_USER}/${REPO}/git/commits" "$COMMIT_BODY"
rm -f "$COMMIT_BODY"
[ "$HTTP_CODE" = "201" ] || die "creating the commit failed (HTTP $HTTP_CODE): $(api_error)"
COMMIT_SHA="$(echo "$HTTP_BODY" | jq -r '.sha')"

REF_BODY="$(mktemp)"
if [ "$REF_EXISTS" = true ]; then
  jq -n --arg sha "$COMMIT_SHA" '{sha:$sha, force:true}' > "$REF_BODY"
  call PATCH "/repos/${GH_USER}/${REPO}/git/refs/heads/${BRANCH}" "$REF_BODY"
  EXPECT=200
else
  jq -n --arg sha "$COMMIT_SHA" --arg ref "refs/heads/${BRANCH}" '{ref:$ref, sha:$sha}' > "$REF_BODY"
  call POST "/repos/${GH_USER}/${REPO}/git/refs" "$REF_BODY"
  EXPECT=201
fi
rm -f "$REF_BODY"
if [ "$HTTP_CODE" != "$EXPECT" ]; then
  case "$(api_error)" in
    *workflow*) die "your token lacks the 'workflow' scope, so .github/workflows could not be pushed. Create a new token with repo + workflow and re-run." ;;
    *) die "updating the branch failed (HTTP $HTTP_CODE): $(api_error)" ;;
  esac
fi
ok "pushed to ${BRANCH} (${COMMIT_SHA:0:7})"

# -------------------------------------------------------------------- pages

echo
bold "4. Turning on the dashboard"
PAGES_URL=""
if [ "$PRIVATE" = true ]; then
  warn "repo is private — GitHub's free plan cannot serve Pages from it"
  warn "alerts will work; the web dashboard will not"
else
  PAGES_BODY="$(mktemp)"
  jq -n --arg b "$BRANCH" '{source:{branch:$b, path:"/docs"}}' > "$PAGES_BODY"
  call POST "/repos/${GH_USER}/${REPO}/pages" "$PAGES_BODY"
  rm -f "$PAGES_BODY"
  case "$HTTP_CODE" in
    201|204) ok "Pages enabled" ;;
    409) warn "Pages was already enabled" ;;
    *) warn "could not enable Pages automatically (HTTP $HTTP_CODE): $(api_error)"
       warn "turn it on by hand: Settings → Pages → main → /docs" ;;
  esac
  PAGES_URL="https://${GH_USER}.github.io/${REPO}/"

  VAR_BODY="$(mktemp)"
  jq -n --arg v "$PAGES_URL" '{name:"RADAR_DASHBOARD_URL", value:$v}' > "$VAR_BODY"
  call POST "/repos/${GH_USER}/${REPO}/actions/variables" "$VAR_BODY"
  rm -f "$VAR_BODY"
  [ "$HTTP_CODE" = "201" ] && ok "alerts will link to the dashboard" || warn "could not set RADAR_DASHBOARD_URL"
fi

# ------------------------------------------------------------------- finish

GH_TOKEN=""   # drop it from the shell's memory

cat <<EOF

$(bold "Done. Three things left — all need your own keys, which I cannot enter for you.")

1. ADD YOUR SECRETS
   https://github.com/${GH_USER}/${REPO}/settings/secrets/actions

   TWELVEDATA_API_KEY   free key from https://twelvedata.com/pricing
   PUSHOVER_TOKEN       the APPLICATION token from pushover.net
   PUSHOVER_USER        your USER key from the pushover.net front page

   Optional, and the reason your positions stay private:
   RADAR_PORTFOLIO_TOML paste the contents of config/portfolio.toml here
                        instead of committing real positions to the repo.

2. RUN IT ONCE
   https://github.com/${GH_USER}/${REPO}/actions/workflows/radar.yml
   Run workflow → tick dry_run → Run.
   Read the log. 16 self-checks run before anything touches the network.
   Then run it again without dry_run to get a real alert.

3. PUT IT ON YOUR PHONE
EOF
if [ -n "$PAGES_URL" ]; then
cat <<EOF
   Open $PAGES_URL in Safari on your iPhone
   (Pages takes a minute or two to go live the first time)
   Share → Add to Home Screen

   Repo:      https://github.com/${GH_USER}/${REPO}
   Dashboard: $PAGES_URL
EOF
else
cat <<EOF
   Not available on a private repo with the free plan.
   Your alerts still arrive via Pushover.

   Repo: https://github.com/${GH_USER}/${REPO}
EOF
fi
echo
