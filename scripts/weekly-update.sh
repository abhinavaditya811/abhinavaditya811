#!/bin/bash
# Weekly README refresh, run by launchd (see scripts/com.readme-activity.plist).
#
# Regenerates the activity block using the local Claude Code login, then commits
# and pushes if anything changed. Safe to run by hand at any time.
#
# Optional overrides go in ~/.config/readme-activity/env, e.g.:
#   GITHUB_TOKEN=ghp_...        # a PAT with repo read scope, to itemize private work
#   NARRATIVE_BACKEND=none      # skip the Claude call entirely

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/readme-activity"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# launchd hands over a near-empty PATH, so name the directories we need.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ENV_FILE="$HOME/.config/readme-activity/env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

cd "$REPO" || { log "FATAL: cannot cd to $REPO"; exit 1; }

if [ -z "${GITHUB_TOKEN:-}" ]; then
  GITHUB_TOKEN="$(gh auth token 2>/dev/null)" || true
  export GITHUB_TOKEN
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
  log "FATAL: no GITHUB_TOKEN. Run 'gh auth login' or set one in $ENV_FILE"
  exit 1
fi
export GITHUB_USER="${GITHUB_USER:-$(gh api user --jq .login 2>/dev/null || echo abhinavaditya811)}"

log "starting refresh for $GITHUB_USER"

# Rebase first so a push from GitHub Actions or another machine doesn't cause a reject.
git fetch --quiet origin || log "WARN: fetch failed, continuing with local state"
if ! git diff --quiet || ! git diff --cached --quiet; then
  log "WARN: working tree is dirty, leaving it alone and bailing out"
  exit 1
fi
git rebase --quiet origin/main 2>/dev/null || log "WARN: rebase skipped"

if ! python3 scripts/update_readme.py; then
  log "FATAL: update_readme.py failed"
  exit 1
fi

if git diff --quiet README.md; then
  log "no change this week, nothing to push"
  exit 0
fi

if [ -n "${DRY_RUN:-}" ]; then
  log "DRY_RUN set, README updated locally but not committed or pushed"
  git --no-pager diff --stat README.md
  exit 0
fi

git add README.md
git commit --quiet -m "chore: refresh README activity"
if git push --quiet origin main; then
  log "pushed $(git rev-parse --short HEAD)"
else
  log "FATAL: push failed. Check that git can authenticate non-interactively."
  exit 1
fi
