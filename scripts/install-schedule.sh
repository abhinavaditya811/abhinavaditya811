#!/bin/bash
# Installs (or reinstalls) the weekly launchd job. Idempotent.
#
# The job runs against its own checkout under ~/.local/share, not your working copy.
# Two reasons: macOS TCC blocks LaunchAgents from reading ~/Downloads, ~/Desktop and
# ~/Documents without Full Disk Access, and a dedicated clone means the scheduled run
# can never collide with uncommitted work in your dev tree.
set -euo pipefail

LABEL="com.readme-activity"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
WORKDIR="$HOME/.local/share/readme-activity/repo"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGIN="$(git -C "$SOURCE" remote get-url origin)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/readme-activity" \
         "$(dirname "$WORKDIR")"

if [ -d "$WORKDIR/.git" ]; then
  echo "Updating existing checkout at $WORKDIR"
  git -C "$WORKDIR" fetch --quiet origin
  git -C "$WORKDIR" reset --quiet --hard origin/main
else
  echo "Cloning into $WORKDIR"
  git clone --quiet "$ORIGIN" "$WORKDIR"
fi
chmod +x "$WORKDIR/scripts/weekly-update.sh"

sed -e "s|__REPO__|$WORKDIR|g" -e "s|__HOME__|$HOME|g" \
    "$SOURCE/scripts/com.readme-activity.plist" > "$TARGET"

# bootout is expected to fail the first time; the job isn't loaded yet.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo
echo "Installed $LABEL (Mondays 09:00), running from $WORKDIR"
echo "  Run now:    launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  Logs:       tail -f ~/Library/Logs/readme-activity/run.log"
echo "  Uninstall:  launchctl bootout gui/$(id -u)/$LABEL && rm $TARGET"
