"""Sync between a user's devices: who is in a sync group, what they share
(the library: playlists, tags, likes, pins; what's playing and where), and
the music files themselves.

A device starts its own group and gets the group's sync code. Another
device asks to join with that code; a device already in the group approves
(or denies) it, and the new device then shares the group's code and data.
Every device holds its own secret token, sent as X-Musify-Sync.

Shared state is kept as whole JSON documents with a revision number: a
device reads it, merges in its own changes and writes it back with the
revision it read; if someone wrote in between, it gets a 409 with the
latest and merges again.

Music files are stored once by their SHA-1 ("blobs") and listed per group
by their path inside the synced folder.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DATA = Path(os.environ.get("MUSIFY_DATA", "/data"))
BLOBS = DATA / "blobs"
# The largest song accepted, in bytes.
MAX_BLOB = int(os.environ.get("MUSIFY_MAX_SONG_MB", "300")) * 1024 * 1024

# No 0/O, 1/I/L: read out and typed by people.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
KINDS = {"library", "playback"}

_db_lock = threading.Lock()
_db = None


class SyncError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def db():
    global _db
    if _db is None:
        DATA.mkdir(parents=True, exist_ok=True)
        BLOBS.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(DATA / "sync.db", check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                code TEXT PRIMARY KEY, created REAL);
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, code TEXT, name TEXT, platform TEXT,
                token TEXT, joined REAL, seen REAL);
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, code TEXT, device TEXT, name TEXT,
                platform TEXT, created REAL, status TEXT, token TEXT);
            CREATE TABLE IF NOT EXISTS state (
                code TEXT, kind TEXT, rev INTEGER, data TEXT, updated REAL,
                device TEXT, PRIMARY KEY (code, kind));
            CREATE TABLE IF NOT EXISTS files (
                code TEXT, path TEXT, sha1 TEXT, size INTEGER, added REAL,
                device TEXT, deleted INTEGER DEFAULT 0,
                PRIMARY KEY (code, path));
            """
        )
    return _db


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _new_code():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _clean_code(code):
    return "".join(c for c in (code or "").upper() if c in _ALPHABET)


def _format(code):
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def _device_fields(body):
    device = str(body.get("device") or "")
    if not 8 <= len(device) <= 64:
        raise SyncError(400, "bad device id")
    name = str(body.get("name") or "Device")[:60]
    platform = str(body.get("platform") or "")[:20]
    return device, name, platform


def _add_device(conn, code, device, name, platform):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("DELETE FROM devices WHERE id = ?", (device,))
    conn.execute(
        "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)",
        (device, code, name, platform, _hash(token), now, now),
    )
    return token


# Groups and devices -----------------------------------------------------------


def create(body):
    """A new group with just this device (leaving any group it was in)."""
    device, name, platform = _device_fields(body)
    with _db_lock:
        conn = db()
        code = _new_code()
        while conn.execute("SELECT 1 FROM groups WHERE code = ?", (code,)).fetchone():
            code = _new_code()
        conn.execute("INSERT INTO groups VALUES (?, ?)", (code, time.time()))
        token = _add_device(conn, code, device, name, platform)
        conn.commit()
    return {"code": code, "token": token}


def join(body):
    """Asks to join the group with this code; a member has to approve."""
    device, name, platform = _device_fields(body)
    code = _format(_clean_code(body.get("code")))
    with _db_lock:
        conn = db()
        if not conn.execute("SELECT 1 FROM groups WHERE code = ?", (code,)).fetchone():
            raise SyncError(404, "No devices use that sync code")
        request = secrets.token_urlsafe(12)
        conn.execute(
            "DELETE FROM requests WHERE device = ? AND status = 'pending'", (device,)
        )
        conn.execute(
            "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
            (request, code, device, name, platform, time.time()),
        )
        conn.commit()
    return {"request": request, "code": code}


def join_status(request):
    """pending, denied, or approved: then the device's token, once."""
    with _db_lock:
        conn = db()
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request,)).fetchone()
        if row is None:
            raise SyncError(404, "No such request")
        result = {"status": row["status"], "code": row["code"]}
        if row["status"] == "approved" and row["token"]:
            result["token"] = row["token"]
            conn.execute("UPDATE requests SET token = NULL WHERE id = ?", (request,))
            conn.commit()
    return result


def device_for(token):
    """The device (row) a token belongs to, seen now."""
    if not token:
        raise SyncError(401, "Not in a sync group")
    with _db_lock:
        conn = db()
        row = conn.execute(
            "SELECT * FROM devices WHERE token = ?", (_hash(token),)
        ).fetchone()
        if row is None:
            raise SyncError(401, "This device was removed from sync")
        conn.execute("UPDATE devices SET seen = ? WHERE id = ?", (time.time(), row["id"]))
        conn.commit()
    return row


def me(device):
    """The group: its code, devices and requests waiting for approval."""
    with _db_lock:
        conn = db()
        devices = conn.execute(
            "SELECT id, name, platform, joined, seen FROM devices WHERE code = ? "
            "ORDER BY joined",
            (device["code"],),
        ).fetchall()
        requests = conn.execute(
            "SELECT id, name, platform, created FROM requests "
            "WHERE code = ? AND status = 'pending' AND created > ? ORDER BY created",
            (device["code"], time.time() - 24 * 3600),
        ).fetchall()
    return {
        "code": device["code"],
        "device": device["id"],
        "devices": [dict(d) | {"me": d["id"] == device["id"]} for d in devices],
        "requests": [dict(r) for r in requests],
    }


def answer(device, request, approve):
    with _db_lock:
        conn = db()
        row = conn.execute(
            "SELECT * FROM requests WHERE id = ? AND code = ? AND status = 'pending'",
            (request, device["code"]),
        ).fetchone()
        if row is None:
            raise SyncError(404, "That request is gone")
        if approve:
            token = _add_device(
                conn, row["code"], row["device"], row["name"], row["platform"]
            )
            conn.execute(
                "UPDATE requests SET status = 'approved', token = ? WHERE id = ?",
                (token, request),
            )
        else:
            conn.execute("UPDATE requests SET status = 'denied' WHERE id = ?", (request,))
        conn.commit()
    return {"ok": True}


def remove(device, target):
    """Takes a device (this one, to leave) out of the group."""
    with _db_lock:
        conn = db()
        conn.execute(
            "DELETE FROM devices WHERE id = ? AND code = ?", (target, device["code"])
        )
        conn.commit()
    return {"ok": True}


# Shared state -----------------------------------------------------------------


def get_state(device, kind):
    if kind not in KINDS:
        raise SyncError(404, "No such state")
    with _db_lock:
        row = db().execute(
            "SELECT rev, data, updated, device FROM state WHERE code = ? AND kind = ?",
            (device["code"], kind),
        ).fetchone()
    if row is None:
        return {"rev": 0, "data": None, "updated": None, "device": None}
    return {
        "rev": row["rev"],
        "data": json.loads(row["data"]),
        "updated": row["updated"],
        "device": row["device"],
    }


def put_state(device, kind, body):
    """Saves it if [rev] is still the latest; else 409 with the latest."""
    if kind not in KINDS:
        raise SyncError(404, "No such state")
    base = int(body.get("rev") or 0)
    data = json.dumps(body.get("data"))
    with _db_lock:
        conn = db()
        row = conn.execute(
            "SELECT rev FROM state WHERE code = ? AND kind = ?", (device["code"], kind)
        ).fetchone()
        current = row["rev"] if row else 0
        if base != current:
            latest = None
        else:
            conn.execute(
                "INSERT OR REPLACE INTO state VALUES (?, ?, ?, ?, ?, ?)",
                (device["code"], kind, current + 1, data, time.time(), device["id"]),
            )
            conn.commit()
            return {"rev": current + 1}
    latest = get_state(device, kind)
    raise SyncError(409, json.dumps(latest))


# Music files -------------------------------------------------------------------


def files(device):
    with _db_lock:
        rows = db().execute(
            "SELECT path, sha1, size, added, device, deleted FROM files WHERE code = ?",
            (device["code"],),
        ).fetchall()
    return {"files": [dict(r) | {"deleted": bool(r["deleted"])} for r in rows]}


def _safe_path(path):
    path = str(path or "").replace("\\", "/").strip("/")
    parts = path.split("/")
    if not path or len(path) > 1000 or any(p in ("", ".", "..") for p in parts):
        raise SyncError(400, f"bad path: {path}")
    return path


def change_files(device, body):
    """Adds (or replaces) and deletes files in the group's folder."""
    now = time.time()
    with _db_lock:
        conn = db()
        for item in body.get("add") or []:
            path = _safe_path(item.get("path"))
            sha1 = str(item.get("sha1") or "").lower()
            if not (len(sha1) == 40 and (BLOBS / sha1).exists()):
                raise SyncError(400, f"upload {path} first")
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, 0)",
                (device["code"], path, sha1, int(item.get("size") or 0), now, device["id"]),
            )
        for path in body.get("delete") or []:
            conn.execute(
                "UPDATE files SET deleted = 1, added = ?, device = ? "
                "WHERE code = ? AND path = ?",
                (now, device["id"], device["code"], _safe_path(path)),
            )
        conn.commit()
    return {"ok": True}


def blob_path(sha1):
    sha1 = (sha1 or "").lower()
    if len(sha1) != 40 or any(c not in "0123456789abcdef" for c in sha1):
        raise SyncError(400, "bad sha1")
    return BLOBS / sha1


def missing_blobs(body):
    """Which of these files the server doesn't have yet."""
    wanted = [str(h).lower() for h in (body.get("sha1s") or [])][:5000]
    return {"missing": [h for h in wanted if not blob_path(h).exists()]}


def save_blob(sha1, stream, length):
    """Stores an upload, if it really is [sha1]."""
    target = blob_path(sha1)
    if length > MAX_BLOB:
        raise SyncError(413, "That song is too big")
    if target.exists():
        # Already here: read the upload anyway, so the connection stays sane.
        while length > 0:
            chunk = stream.read(min(length, 1 << 20))
            if not chunk:
                break
            length -= len(chunk)
        return {"ok": True}
    part = target.with_suffix(f".{secrets.token_hex(4)}.part")
    digest = hashlib.sha1()
    left = length
    with part.open("wb") as f:
        while left > 0:
            chunk = stream.read(min(left, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            f.write(chunk)
            left -= len(chunk)
    if left or digest.hexdigest() != target.name:
        part.unlink(missing_ok=True)
        raise SyncError(400, "The upload was cut off or changed on the way")
    part.replace(target)
    return {"ok": True}
