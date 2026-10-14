#!/bin/sh
# The newest yt-dlp and ytmusicapi on every start: YouTube breaks old ones.
# The server exits once a day when idle, so this runs daily
# (restart: unless-stopped).
pip install --no-cache-dir -q -U -r requirements.txt || echo "update failed, using the installed versions"
exec python -u musify_server.py
