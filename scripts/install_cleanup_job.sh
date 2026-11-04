#!/bin/sh
# Install (or remove with --uninstall) a macOS launchd job that runs scripts/cleanup.sh
# at 03:00 on the 1st of every month. Runs on the host because only the host should
# control Docker (giving a container the Docker socket would give it root on the Mac).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LABEL=com.family-event-bot.cleanup
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$PLIST"
    echo "Removed $LABEL"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/sh</string><string>$ROOT/scripts/cleanup.sh</string></array>
    <key>StartCalendarInterval</key>
    <dict><key>Day</key><integer>1</integer><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/family-event-bot-cleanup.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/family-event-bot-cleanup.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL: runs $ROOT/scripts/cleanup.sh at 03:00 on the 1st of each month."
echo "Run it now:  launchctl kickstart gui/$(id -u)/$LABEL"
