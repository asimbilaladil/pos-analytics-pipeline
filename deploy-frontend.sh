#!/usr/bin/env bash
# Build the React app and publish it to the nginx web root.
# Kept out of /root because nginx runs as www-data and /root stays mode 700.
set -euo pipefail
cd "$(dirname "$0")/frontend"
npm run build
rsync -a --delete dist/ /var/www/laynes-intelligence/
chown -R root:www-data /var/www/laynes-intelligence
chmod -R 750 /var/www/laynes-intelligence
find /var/www/laynes-intelligence -type f -exec chmod 640 {} +
echo "published to /var/www/laynes-intelligence"
