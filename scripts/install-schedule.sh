#!/bin/bash
# Installs (or reinstalls) the weekly launchd job. Idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.readme-activity"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/readme-activity"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/com.readme-activity.plist" > "$TARGET"

# bootout is expected to fail the first time; the job isn't loaded yet.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"

echo "Installed $LABEL (Mondays 09:00)."
echo "  Run now:    launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  Status:     launchctl print gui/$(id -u)/$LABEL | head -20"
echo "  Logs:       tail -f ~/Library/Logs/readme-activity/run.log"
echo "  Uninstall:  launchctl bootout gui/$(id -u)/$LABEL && rm $TARGET"
