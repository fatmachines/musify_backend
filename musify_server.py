"""Musify's server: YouTube songs as M4A files, and YouTube playlists
(the charts) as track lists, both through yt-dlp.

The phone can't download from YouTube itself (YouTube only serves its own
player), so it asks this server, which runs yt-dlp with a JavaScript runtime
and keeps yt-dlp up to date (see RESTART_AFTER). See README.md.

Every request needs the key from MUSIFY_KEY, as an X-Musify-Key header or
?key=. Endpoints:

  GET /health                     -> {"ok": true, "ytdlp": "<version>"}
  GET /charts                     -> {"charts": [playlist, ...]}: Global,
                                     US, UK, Canada, Australia, India
  GET /playlist?id=<playlist id>  -> playlist
  GET /spotify?id=<playlist id>   -> a public Spotify playlist, its songs
                                     found on YouTube Music (+ "missing")
  GET /search?q=<query>           -> {"artist": artist or null, "songs":
                                     [track, ...]}: YouTube Music songs only,
                                     and the artist if the query names one
  GET /artist?id=<channel id>     -> artist with "tracks": all their songs,
                                     and "albums"
  GET /album?id=<album id>        -> album with "tracks"
  /sync/...                       -> sync between devices (see sync.py)
  GET /audio/<video id>.m4a       -> the song (byte ranges too, for
                                     streaming): AAC as YouTube has it, not
                                     re-encoded, with title, artist and cover

A playlist is {"id", "title", "tracks": [track, ...]}; a track is {"id",
"title", "channel", "duration" (seconds), "thumbnail"}, plus "album" and
"artistId" from YouTube Music. An artist is {"id", "name", "thumbnail"}; an
album is {"id", "title", "year", "thumbnail"}.
"""

import hmac
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from ytmusicapi import YTMusic

import sync

KEY = os.environ["MUSIFY_KEY"]
HOST = os.environ.get("MUSIFY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MUSIFY_PORT", "8790"))
CACHE = Path(os.environ.get("MUSIFY_CACHE", "/var/cache/musify"))
NODE = os.environ.get("MUSIFY_NODE") or shutil.which("node")
# Files kept for re-downloads, at most this much in all.
CACHE_LIMIT = int(os.environ.get("MUSIFY_CACHE_MB", "2000")) * 1024 * 1024
PLAYLIST_TTL = 60 * 60
# YouTube changes often and old yt-dlp versions stop working, so the server
# exits about once a day, when idle, to be restarted with the newest
# yt-dlp (see entrypoint.sh).
RESTART_AFTER = 24 * 60 * 60

# YouTube Music's own charts (channel "YouTube Music Global Charts").
CHARTS = [
    ("PL4fGSI1pDJn6puJdseH2Rt9sMvt9E2M4i", "Top 100 Songs Global"),
    ("PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI", "Top 100 Songs United States"),
    ("PL4fGSI1pDJn6_f5P3MnzXg9l3GDfnSlXa", "Top 100 Songs United Kingdom"),
    ("PL4fGSI1pDJn57Q7WbODbmXjyjgXi0BTyD", "Top 100 Songs Canada"),
    ("PL4fGSI1pDJn7xvYy-bP6UFeG5tITQgScd", "Top 100 Songs Australia"),
    ("PL4fGSI1pDJn4pTWyM3t61lOyZ6_4jcNOw", "Top 100 Songs India"),
]

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def base_options():
    options = {"quiet": True, "no_warnings": True, "noprogress": True}
    if NODE:
        options["js_runtimes"] = {"node": {"path": NODE}}
    return options


# Playlists ------------------------------------------------------------------

_playlists = {}
_playlists_lock = threading.Lock()


def playlist(playlist_id, title=None, fresh=False):
    """The playlist's tracks, fetched at most once an hour (or now, if
    [fresh])."""
    with _playlists_lock:
        cached = _playlists.get(playlist_id)
    if not fresh and cached and time.time() - cached[0] < PLAYLIST_TTL:
        return cached[1]
    options = base_options() | {"extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/playlist?list={playlist_id}", download=False
        )
    tracks = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        # Gone from YouTube, but still listed.
        if entry.get("title") in ("[Private video]", "[Deleted video]"):
            continue
        thumbnails = entry.get("thumbnails") or []
        channel = entry.get("channel") or entry.get("uploader")
        if channel:
            # YouTube Music's auto-made channels: "Queen - Topic".
            channel = re.sub(r" - Topic$", "", channel)
        tracks.append(
            {
                "id": entry["id"],
                "title": entry.get("title") or "",
                "channel": channel,
                "duration": entry.get("duration"),
                "thumbnail": f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg"
                if not thumbnails
                else thumbnails[-1].get("url"),
            }
        )
    result = {"id": playlist_id, "title": title or info.get("title"), "tracks": tracks}
    with _playlists_lock:
        _playlists[playlist_id] = (time.time(), result)
    log("playlist", playlist_id, len(tracks), "tracks")
    return result


def charts(fresh=False):
    # All at once: one by one takes a few seconds each.
    with ThreadPoolExecutor(len(CHARTS)) as pool:
        return list(pool.map(lambda c: playlist(*c, fresh=fresh), CHARTS))


def keep_charts_warm():
    """Refreshes the charts before they expire, so the app never waits."""
    while True:
        try:
            charts(fresh=True)
        except Exception as e:  # noqa: BLE001 - try again next time
            log("charts refresh failed:", repr(e))
        time.sleep(PLAYLIST_TTL - 10 * 60)


# YouTube Music -------------------------------------------------------------


def _big(thumbnails, size=544):
    """The largest thumbnail, asked for at [size] (Google's image URLs take
    the size in the URL)."""
    if not thumbnails:
        return None
    url = thumbnails[-1].get("url")
    return re.sub(r"=w\d+-h\d+", f"=w{size}-h{size}", url) if url else None


def _music_track(item):
    artists = [a for a in item.get("artists") or [] if a.get("name")]
    album = item.get("album") or {}
    # Album tracks name their album as plain text.
    if isinstance(album, str):
        album = {"name": album}
    return {
        "id": item["videoId"],
        "title": item.get("title") or "",
        "channel": ", ".join(a["name"] for a in artists) or None,
        "artistId": next((a["id"] for a in artists if a.get("id")), None),
        "album": album.get("name"),
        "duration": item.get("duration_seconds"),
        "thumbnail": _big(item.get("thumbnails")),
    }


def _plain(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def search(query):
    """YouTube Music's songs for [query] (no videos, mixes or podcasts), and
    the artist, when the query is (nearly) their name."""
    music = YTMusic()
    with ThreadPoolExecutor(2) as pool:
        songs = pool.submit(music.search, query, filter="songs", limit=40)
        artists = pool.submit(music.search, query, filter="artists", limit=3)
    tracks = [_music_track(s) for s in songs.result() if s.get("videoId")]
    # Their name, and they sing the top songs: "shape of you" shouldn't
    # find a channel that happens to be called that.
    singers = {t["artistId"] for t in tracks[:5] if t["artistId"]}
    artist = None
    wanted = _plain(query)
    for found in artists.result():
        name = found.get("artist") or ""
        if found.get("browseId") in singers and wanted and _plain(name) and (
            wanted == _plain(name)
            or (len(wanted) >= 4 and _plain(name).startswith(wanted))
        ):
            artist = {
                "id": found["browseId"],
                "name": name,
                "thumbnail": _big(found.get("thumbnails")),
            }
            break
    return {"artist": artist, "songs": tracks}


_artists = {}
_artists_lock = threading.Lock()


def artist(artist_id):
    """The artist with all their songs (their "Songs" list on YouTube
    Music), fetched at most once an hour."""
    with _artists_lock:
        cached = _artists.get(artist_id)
    if cached and time.time() - cached[0] < PLAYLIST_TTL:
        return cached[1]
    music = YTMusic()
    info = music.get_artist(artist_id)
    songs = info.get("songs") or {}
    items = songs.get("results") or []
    if songs.get("browseId"):
        try:
            items = music.get_playlist(songs["browseId"], limit=None)["tracks"]
        except Exception as e:  # noqa: BLE001 - the top songs will do
            log("artist songs failed:", repr(e))
    albums = info.get("albums") or {}
    album_items = albums.get("results") or []
    if albums.get("browseId"):
        try:
            album_items = music.get_artist_albums(
                albums["browseId"], albums.get("params")
            )
        except Exception as e:  # noqa: BLE001 - the first few will do
            log("artist albums failed:", repr(e))
    result = {
        "id": artist_id,
        "name": info.get("name") or "",
        "thumbnail": _big(info.get("thumbnails")),
        "tracks": [_music_track(t) for t in items if t.get("videoId")],
        "albums": [
            {
                "id": a["browseId"],
                "title": a.get("title") or "",
                "year": a.get("year"),
                "thumbnail": _big(a.get("thumbnails")),
            }
            for a in album_items
            if a.get("browseId")
        ],
    }
    with _artists_lock:
        _artists[artist_id] = (time.time(), result)
    log("artist", artist_id, len(result["tracks"]), "songs")
    return result


_albums = {}


def album(album_id):
    """The album's songs (those that can be played), cached for an hour."""
    with _artists_lock:
        cached = _albums.get(album_id)
    if cached and time.time() - cached[0] < PLAYLIST_TTL:
        return cached[1]
    info = YTMusic().get_album(album_id)
    cover = _big(info.get("thumbnails"))
    tracks = []
    for item in info.get("tracks") or []:
        if not item.get("videoId"):
            continue
        track = _music_track(item)
        track["album"] = track["album"] or info.get("title")
        # Album tracks have no pictures of their own: the album's.
        track["thumbnail"] = track["thumbnail"] or cover
        tracks.append(track)
    artists = info.get("artists") or []
    result = {
        "id": album_id,
        "title": info.get("title") or "",
        "year": info.get("year"),
        "artist": ", ".join(a["name"] for a in artists if a.get("name")) or None,
        "thumbnail": cover,
        "tracks": tracks,
    }
    with _artists_lock:
        _albums[album_id] = (time.time(), result)
    log("album", album_id, len(tracks), "songs")
    return result


# Spotify ----------------------------------------------------------------------

SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

_spotify = {}
_spotify_lock = threading.Lock()


def _fetch_text(url):
    request = urllib.request.Request(url, headers=_BROWSER)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


# The query Spotify's web player reads a playlist with. Spotify can retire
# it; then only playlists of up to 100 songs import (from the embed page).
SPOTIFY_PLAYLIST_QUERY = (
    "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"
)


def _spotify_page(playlist_id, token, offset):
    """[offset] onwards of the playlist, up to 100 songs, and how many it
    has, through Spotify's web player API."""
    query = urllib.parse.urlencode(
        {
            "operationName": "fetchPlaylist",
            "variables": json.dumps(
                {
                    "uri": f"spotify:playlist:{playlist_id}",
                    "offset": offset,
                    "limit": 100,
                    "enableWatchFeedEntrypoint": False,
                }
            ),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": SPOTIFY_PLAYLIST_QUERY}}
            ),
        }
    )
    request = urllib.request.Request(
        f"https://api-partner.spotify.com/pathfinder/v1/query?{query}",
        headers=_BROWSER | {"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = json.load(response)["data"]["playlistV2"]["content"]
    songs = []
    for item in content.get("items") or []:
        data = (item.get("itemV2") or {}).get("data") or {}
        if not data.get("name"):
            continue  # a podcast episode, or gone from Spotify
        artists = ((data.get("artists") or {}).get("items")) or []
        songs.append(
            {
                "title": data["name"],
                "artists": ", ".join(
                    a["profile"]["name"] for a in artists if a.get("profile")
                ),
                "duration": ((data.get("trackDuration") or {}).get("totalMilliseconds") or 0)
                / 1000,
            }
        )
    return songs, content.get("totalCount") or 0


def _spotify_songs(playlist_id):
    """A public Spotify playlist's name and all its songs (title, artists,
    length), without a Spotify account: the embed page gives the name, the
    first 100 songs and a token for the web player's API, which pages
    through the rest. Fails rather than give part of a playlist."""
    page = _fetch_text(f"https://open.spotify.com/embed/playlist/{playlist_id}")
    found = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    if not found:
        raise RuntimeError("Spotify didn't show that playlist (is it public?)")
    state = json.loads(found[1])["props"]["pageProps"]["state"]
    entity = state["data"]["entity"]
    name = entity.get("name") or "Spotify playlist"
    first = [
        {
            "title": t["title"],
            "artists": t.get("subtitle") or "",
            "duration": (t.get("duration") or 0) / 1000,
        }
        for t in entity.get("trackList") or []
        if t.get("title")
    ]
    token = ((state.get("settings") or {}).get("session") or {}).get("accessToken")
    try:
        songs, total = _spotify_page(playlist_id, token, 0)
        while len(songs) < total:
            more, _ = _spotify_page(playlist_id, token, len(songs))
            if not more:
                break
            songs += more
        if songs:
            return name, songs
    except Exception as e:  # noqa: BLE001 - the embed page may do
        log("spotify: web player API failed:", repr(e))
    # The embed page lists up to 100: enough only for a playlist that size.
    if len(first) < 100:
        return name, first
    raise RuntimeError(
        "Couldn't read the whole playlist from Spotify, so nothing was imported"
    )


def _on_youtube(song):
    """The song on YouTube Music: of the first few results for its title
    and artists, the one whose length, title and artist agree best; None if
    none does."""
    results = YTMusic().search(
        f"{song['title']} {song['artists']}", filter="songs", limit=5
    )
    title = _plain(re.sub(r"\s*[(\[].*?[)\]]", "", song["title"]).split(" - ")[0])
    artists = {_plain(a) for a in song["artists"].split(",") if _plain(a)}
    best, best_score = None, 0.5
    for rank, item in enumerate(r for r in results if r.get("videoId")):
        track = _music_track(item)
        score = -0.5 * rank  # YouTube Music's own order counts a little
        if track["duration"] and song["duration"]:
            off = abs(track["duration"] - song["duration"])
            score += 3 if off <= 3 else 2 if off <= 8 else 0 if off <= 20 else -3
        if title and title in _plain(track["title"]):
            score += 2
        channel = _plain(track["channel"] or "")
        if any(a in channel or channel in a for a in artists if channel):
            score += 2
        if score > best_score:
            best, best_score = track, score
    return best


def spotify_playlist(playlist_id):
    """A public Spotify playlist with each song found on YouTube Music, as a
    playlist like YouTube's, plus the songs that weren't found ("missing").
    Kept for an hour."""
    with _spotify_lock:
        cached = _spotify.get(playlist_id)
    if cached and time.time() - cached[0] < PLAYLIST_TTL:
        return cached[1]
    started = time.time()
    name, songs = _spotify_songs(playlist_id)

    def find(song):
        try:
            return _on_youtube(song)
        except Exception as e:  # noqa: BLE001 - counts as not found
            log("spotify: search failed:", repr(e))
            return None

    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(find, songs))
    tracks, missing, seen = [], [], set()
    for song, track in zip(songs, found):
        if track is None:
            missing.append(f"{song['title']} – {song['artists']}")
        elif track["id"] not in seen:
            seen.add(track["id"])
            tracks.append(track)
    result = {
        "id": f"spotify-{playlist_id}",
        "title": name,
        "tracks": tracks,
        "missing": missing,
    }
    with _spotify_lock:
        _spotify[playlist_id] = (time.time(), result)
    log(
        "spotify", playlist_id, f"{len(tracks)}/{len(songs)} found",
        f"in {time.time() - started:.1f}s",
    )
    return result


# Songs ----------------------------------------------------------------------

_song_locks = {}
_song_locks_lock = threading.Lock()
# YouTube fetches at once: more wait their turn, so a burst of requests
# (an app asking for a whole playlist) can't swamp the server.
_fetch_slots = threading.BoundedSemaphore(2)


def song_file(video_id):
    """The song as a tagged M4A in the cache, downloading it if needed."""
    path = CACHE / f"{video_id}.m4a"
    with _song_locks_lock:
        lock = _song_locks.setdefault(video_id, threading.Lock())
    with lock:
        if path.exists():
            os.utime(path)
            return path
        work = CACHE / "work" / video_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        options = base_options() | {
            # AAC in MP4 only: it has to play on an iPhone.
            "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]",
            "outtmpl": str(work / "song.%(ext)s"),
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
        with _fetch_slots:
            started = time.time()
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        done = work / "song.m4a"
        if not done.exists():
            raise RuntimeError("yt-dlp made no m4a")
        done.replace(path)
        shutil.rmtree(work, ignore_errors=True)
        log("song", video_id, path.stat().st_size, "bytes in",
            f"{time.time() - started:.1f}s")
    trim_cache()
    return path


def trim_cache():
    files = sorted(CACHE.glob("*.m4a"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > CACHE_LIMIT:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)


# HTTP -----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "Musify"

    def log_message(self, format, *args):
        # Without the query string: it can hold the key.
        log(self.address_string(), (format % args).replace(
            self.path, self.path.split("?")[0]))

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self, query):
        given = self.headers.get("X-Musify-Key") or query.get("key", [""])[0]
        return hmac.compare_digest(given.encode(), KEY.encode())

    def _handle(self, method):
        global _busy
        with _busy_lock:
            _busy += 1
        try:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if not self._authorised(query):
                return self._send_json(401, {"error": "wrong key"})
            if url.path.startswith("/sync/"):
                return self._sync(method, url.path)
            if method != "GET":
                return self._send_json(405, {"error": "use GET"})
            return self._get(url, query)
        finally:
            with _busy_lock:
                _busy -= 1

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20 * 1024 * 1024:
            raise sync.SyncError(413, "too big")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise sync.SyncError(400, "bad JSON") from None

    def _sync(self, method, path):
        """Sync between devices: see sync.py."""
        try:
            parts = path.strip("/").split("/")[1:]
            if method == "POST" and parts == ["create"]:
                return self._send_json(200, sync.create(self._body()))
            if method == "POST" and parts == ["join"]:
                return self._send_json(200, sync.join(self._body()))
            if method == "GET" and len(parts) == 2 and parts[0] == "join":
                return self._send_json(200, sync.join_status(parts[1]))

            device = sync.device_for(self.headers.get("X-Musify-Sync"))
            if method == "GET" and parts == ["me"]:
                return self._send_json(200, sync.me(device))
            if method == "POST" and len(parts) == 2 and parts[0] == "requests":
                approve = bool(self._body().get("approve"))
                return self._send_json(200, sync.answer(device, parts[1], approve))
            if method == "DELETE" and len(parts) == 2 and parts[0] == "devices":
                return self._send_json(200, sync.remove(device, parts[1]))
            if len(parts) == 2 and parts[0] == "state":
                if method == "GET":
                    return self._send_json(200, sync.get_state(device, parts[1]))
                if method == "PUT":
                    body = self._body()
                    return self._send_json(
                        200, sync.put_state(device, parts[1], body)
                    )
            if parts == ["files"]:
                if method == "GET":
                    return self._send_json(200, sync.files(device))
                if method == "POST":
                    return self._send_json(
                        200, sync.change_files(device, self._body())
                    )
            if method == "POST" and parts == ["have"]:
                return self._send_json(200, sync.set_holdings(device, self._body()))
            if method == "POST" and parts == ["blobs", "missing"]:
                return self._send_json(200, sync.missing_blobs(self._body()))
            if len(parts) == 2 and parts[0] == "blobs":
                blob = sync.blob_path(parts[1])
                if method == "GET":
                    if not blob.exists():
                        return self._send_json(404, {"error": "not uploaded"})
                    return self._send_file(blob)
                if method == "PUT":
                    length = int(self.headers.get("Content-Length") or 0)
                    return self._send_json(
                        200, sync.save_blob(parts[1], self.rfile, length)
                    )
            return self._send_json(404, {"error": "not found"})
        except sync.SyncError as e:
            if e.status == 409:
                return self._send_json(409, {"error": "changed", "latest": json.loads(e.message)})
            return self._send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001 - report anything to the app
            log("sync failed:", repr(e))
            return self._send_json(500, {"error": str(e)})

    def _get(self, url, query):
        try:
            if url.path == "/health":
                return self._send_json(
                    200, {"ok": True, "ytdlp": yt_dlp.version.__version__}
                )
            if url.path == "/charts":
                return self._send_json(200, {"charts": charts()})
            if url.path == "/playlist":
                playlist_id = query.get("id", [""])[0]
                if not PLAYLIST_ID.match(playlist_id):
                    return self._send_json(400, {"error": "bad playlist id"})
                return self._send_json(200, playlist(playlist_id))
            if url.path == "/spotify":
                playlist_id = query.get("id", [""])[0]
                if not SPOTIFY_ID.match(playlist_id):
                    return self._send_json(400, {"error": "bad Spotify playlist id"})
                return self._send_json(200, spotify_playlist(playlist_id))
            if url.path == "/search":
                query_text = query.get("q", [""])[0].strip()
                if not query_text or len(query_text) > 200:
                    return self._send_json(400, {"error": "bad query"})
                return self._send_json(200, search(query_text))
            if url.path == "/artist":
                artist_id = query.get("id", [""])[0]
                if not PLAYLIST_ID.match(artist_id):
                    return self._send_json(400, {"error": "bad artist id"})
                return self._send_json(200, artist(artist_id))
            if url.path == "/album":
                album_id = query.get("id", [""])[0]
                if not PLAYLIST_ID.match(album_id):
                    return self._send_json(400, {"error": "bad album id"})
                return self._send_json(200, album(album_id))
            match = re.fullmatch(r"/audio/([^/]+)\.m4a", url.path)
            if match:
                if not VIDEO_ID.match(match[1]):
                    return self._send_json(400, {"error": "bad video id"})
                return self._send_file(song_file(match[1]))
            return self._send_json(404, {"error": "not found"})
        except yt_dlp.utils.DownloadError as e:
            log("yt-dlp:", e)
            return self._send_json(502, {"error": str(e).replace("ERROR: ", "")})
        except Exception as e:  # noqa: BLE001 - report anything to the app
            log("failed:", repr(e))
            return self._send_json(500, {"error": str(e)})

    def _send_file(self, path):
        """The file, or the byte range asked for: the iPhone's and Mac's
        player streams a song only from a server that answers ranges."""
        size = path.stat().st_size
        start, end = 0, size - 1
        ranged = False
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                if match[2]:
                    end = min(int(match[2]), size - 1)
            else:  # the last N bytes
                start = max(0, size - int(match[2]))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ranged = True
        self.send_response(206 if ranged else 200)
        self.send_header(
            "Content-Type",
            "audio/mp4" if path.suffix == ".m4a" else "application/octet-stream",
        )
        self.send_header("Accept-Ranges", "bytes")
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(64 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


_busy = 0
_busy_lock = threading.Lock()


def restart_daily():
    started = time.time()
    while True:
        time.sleep(60)
        with _busy_lock:
            if time.time() - started > RESTART_AFTER and _busy == 0:
                log("restarting for a yt-dlp update")
                os._exit(0)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    log("musify server on %s:%d, yt-dlp %s, node %s"
        % (HOST, PORT, yt_dlp.version.__version__, NODE))
    threading.Thread(target=restart_daily, daemon=True).start()
    threading.Thread(target=keep_charts_warm, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
