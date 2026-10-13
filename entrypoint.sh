#!/bin/sh
# The newest yt-dlp on every start: YouTube breaks old ones. The server
# exits once a day when idle, so this runs daily (restart: unless-stopped).
pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp update failed, using the installed one"
exec python -u musify_server.py
