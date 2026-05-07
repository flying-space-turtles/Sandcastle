"""Vulnerable notes service.

This is the team's vulnerable application. It exposes a tiny REST API that
lets users register, log in, and store notes. The intended attack vector is
an IDOR (Insecure Direct Object Reference) on the `/api/notes` endpoint:
the listing endpoint returns *every* note in the database regardless of the
caller's identity, so an attacker can read flags planted in other users'
notes.

The CTF gameserver plants flags by registering as a "checker" user and
posting a note containing the flag. The SLA checker reads it back via
`/api/note/<id>` to verify the service is functional and the flag is intact.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from typing import Any

from flask import Flask, g, jsonify, request

DB_PATH = os.environ.get("NOTES_DB_PATH", "/app/data/notes.sqlite")
TEAM_NAME = os.environ.get("TEAM_NAME", "unknown")

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(_exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            token TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def serialise_note(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


@app.get("/")
def index() -> Any:
    return jsonify(
        {
            "service": "notes",
            "team": TEAM_NAME,
            "endpoints": [
                "POST /api/register",
                "POST /api/notes (Authorization: Bearer <token>)",
                "GET  /api/note/<id>",
                "GET  /api/notes  (vulnerable: lists ALL notes)",
            ],
        }
    )


@app.get("/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.post("/api/register")
def register() -> Any:
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    if not username or len(username) > 64:
        return jsonify({"error": "invalid username"}), 400

    token = uuid.uuid4().hex
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, token, created_at) VALUES (?, ?, ?)",
            (username, token, time.time()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "username already exists"}), 409
    return jsonify({"id": cur.lastrowid, "username": username, "token": token}), 201


def auth_user() -> sqlite3.Row | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE token = ?", (token,)
    ).fetchone()


@app.post("/api/notes")
def create_note() -> Any:
    user = auth_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    content = payload.get("content") or ""
    if not title or len(title) > 256:
        return jsonify({"error": "invalid title"}), 400
    if not isinstance(content, str) or len(content) > 8192:
        return jsonify({"error": "invalid content"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO notes (owner_id, title, content, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], title, content, time.time()),
    )
    db.commit()
    row = db.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(serialise_note(row)), 201


@app.get("/api/note/<int:note_id>")
def read_note(note_id: int) -> Any:
    # Note: intentionally does NOT check ownership. The gameserver SLA checker
    # relies on this to read flags it planted. (IDOR is the primary
    # vulnerability of the service.)
    row = get_db().execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(serialise_note(row))


@app.get("/api/notes")
def list_notes() -> Any:
    # IDOR: returns every note in the database, not just the caller's.
    # An attacker with no credentials can list flags planted by the gameserver.
    rows = get_db().execute(
        "SELECT * FROM notes ORDER BY id DESC LIMIT 200"
    ).fetchall()
    return jsonify([serialise_note(r) for r in rows])


@app.post("/api/admin/reset")
def admin_reset() -> Any:
    # Used by the gameserver for `reset.sh` — wipes all notes and users.
    secret = request.headers.get("X-Admin-Token", "")
    expected = os.environ.get("NOTES_ADMIN_TOKEN", "change-me")
    if secret != expected:
        return jsonify({"error": "forbidden"}), 403
    db = get_db()
    db.execute("DELETE FROM notes")
    db.execute("DELETE FROM users")
    db.commit()
    return jsonify({"status": "reset"})


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
