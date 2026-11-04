#!/bin/sh
# Docker housekeeping for the bot host. Runs monthly via launchd (scripts/install_cleanup_job.sh),
# or by hand any time. Safe: removes only untagged image layers left by rebuilds and build cache
# older than 7 days. Never touches running containers, tagged images, volumes or data/.
# Writes a short report to data/docker-disk.txt, which the bot shows on /storage.
set -eu
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M') Docker is not running; skipped." >&2
    exit 0
fi

images=$(docker image prune -f | tail -1)
cache=$(docker builder prune -f --filter until=168h | tail -1)
{
    echo "Cleaned $(date '+%Y-%m-%d %H:%M')"
    echo "Old image layers: $(echo "$images" | sed 's/^Total reclaimed space:[[:space:]]*//') freed"
    echo "Build cache: $(echo "$cache" | sed 's/^Total:[[:space:]]*//') freed"
    echo
    docker system df --format '{{.Type}}: {{.Size}} ({{.Reclaimable}} reclaimable)'
    echo
    echo "Bot data folder: $(du -sh data | cut -f1)"
} > data/docker-disk.txt.tmp
mv data/docker-disk.txt.tmp data/docker-disk.txt
cat data/docker-disk.txt
