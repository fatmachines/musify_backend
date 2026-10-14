FROM python:3.13-slim

# ffmpeg tags the files and embeds covers; Node runs YouTube's JavaScript
# challenges for yt-dlp.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements.txt musify_server.py entrypoint.sh ./

ENV MUSIFY_HOST=0.0.0.0 MUSIFY_PORT=8790 MUSIFY_CACHE=/cache
CMD ["./entrypoint.sh"]
