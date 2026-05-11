"""TurtleNotes - Sandcastle template vulnerable web application.

This is an intentionally insecure Flask app used as the per-team challenge
service for the Sandcastle Attack & Defense scaffold. It exposes three
deliberate vulnerabilities (SQL injection, command injection, and path
traversal) on top of an otherwise functional notes API.

Do NOT deploy this anywhere reachable from untrusted networks.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "app.db")))
FLAG_FILE = Path(os.environ.get("FLAG_FILE", str(DATA_DIR / "flag.txt")))
NOTES_DIR = Path(os.environ.get("NOTES_DIR", str(DATA_DIR / "notes")))

TEAM_ID = os.environ.get("TEAM_ID", "0")
TEAM_NAME = os.environ.get("TEAM_NAME", "Team 0")
SECRET_KEY = os.environ.get("SECRET_KEY", "sandcastle-default-secret-change-me")
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("turtlenotes")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = SECRET_KEY
    app.config["TEAM_ID"] = TEAM_ID
    app.config["TEAM_NAME"] = TEAM_NAME

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    init_db(app)

    register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL UNIQUE,
    password  TEXT NOT NULL,
    is_admin  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id  INTEGER NOT NULL REFERENCES users(id),
    title     TEXT NOT NULL,
    body      TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_database", None)
    if db is None:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        g._database = db
    return db


def init_db(app: Flask) -> None:
    """Create the schema and seed an admin account with a starter flag."""
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()

        cur = db.execute("SELECT id FROM users WHERE username = ?", ("admin",))
        if cur.fetchone() is None:
            admin_password = os.environ.get(
                "ADMIN_PASSWORD",
                f"adm1n-{secrets.token_hex(8)}",
            )
            db.execute(
                "INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1)",
                ("admin", admin_password),
            )
            db.commit()
            logger.info("seeded admin account (password not logged)")

        cur = db.execute("SELECT id FROM users WHERE username = ?", ("guest",))
        if cur.fetchone() is None:
            db.execute(
                "INSERT INTO users (username, password, is_admin) VALUES (?, ?, 0)",
                ("guest", "guest"),
            )
            db.commit()

        flag = ensure_flag()
        admin_id = db.execute(
            "SELECT id FROM users WHERE username = 'admin'"
        ).fetchone()["id"]
        cur = db.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND is_secret = 1",
            (admin_id,),
        )
        if cur.fetchone() is None:
            db.execute(
                "INSERT INTO notes (owner_id, title, body, is_secret) "
                "VALUES (?, ?, ?, 1)",
                (
                    admin_id,
                    "Round 0 flag",
                    f"The current flag is: {flag}",
                ),
            )
            db.commit()

        # A few public notes so the homepage is not empty.
        if (
            db.execute(
                "SELECT COUNT(*) AS c FROM notes WHERE is_secret = 0"
            ).fetchone()["c"]
            == 0
        ):
            seed_public_notes(db, admin_id)


def seed_public_notes(db: sqlite3.Connection, admin_id: int) -> None:
    samples: Iterable[tuple[str, str]] = (
        (
            "Welcome to TurtleNotes",
            "Tiny notes service for the Sandcastle CTF. Register an account "
            "and start writing!",
        ),
        (
            "Operations",
            "Operators: use /admin/diagnostics to ping a host from inside the "
            "container.",
        ),
        (
            "Backups",
            "Note exports are stored under /app/data/notes and can be fetched "
            "via /export.",
        ),
    )
    for title, body in samples:
        db.execute(
            "INSERT INTO notes (owner_id, title, body, is_secret) VALUES (?, ?, ?, 0)",
            (admin_id, title, body),
        )
    db.commit()


def ensure_flag() -> str:
    """Read the planted flag from disk, creating one on first boot."""
    if FLAG_FILE.exists():
        return FLAG_FILE.read_text().strip()

    flag = f"FLAG{{{secrets.token_hex(16)}}}"
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(flag + "\n")
    logger.info("planted initial flag at %s", FLAG_FILE)
    return flag


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_routes(app: Flask) -> None:
    @app.teardown_appcontext
    def _close_db(exception):  # noqa: ANN001 - Flask signature
        db = getattr(g, "_database", None)
        if db is not None:
            db.close()

    @app.context_processor
    def _inject_team():
        return {
            "team_id": app.config["TEAM_ID"],
            "team_name": app.config["TEAM_NAME"],
            "current_user": session.get("username"),
        }

    @app.get("/")
    def index():
        db = get_db()
        rows = db.execute(
            "SELECT n.id, n.title, n.body, u.username AS author, n.created_at "
            "FROM notes n JOIN users u ON u.id = n.owner_id "
            "WHERE n.is_secret = 0 ORDER BY n.id DESC LIMIT 25"
        ).fetchall()
        return render_template("index.html", notes=rows)

    @app.get("/health")
    def health():
        logger.info("health check from %s", request.remote_addr)
        return {"status": "ok"}

    # -- auth ---------------------------------------------------------------

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if not username or not password:
                flash("username and password are required", "error")
                return render_template("register.html"), 400
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, password),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("username already taken", "error")
                return render_template("register.html"), 409
            flash("account created, please log in", "info")
            return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username") or ""
            password = request.form.get("password") or ""

            # VULN: SQL injection. Credentials are concatenated directly into
            # the query, allowing payloads like:
            #     username = admin' --
            #     username = ' OR '1'='1' --
            db = get_db()
            query = (
                "SELECT id, username, is_admin FROM users "
                f"WHERE username = '{username}' AND password = '{password}' "
                "LIMIT 1"
            )
            logger.info("login query: %s", query)
            try:
                row = db.execute(query).fetchone()
            except sqlite3.Error as exc:
                flash(f"login error: {exc}", "error")
                return render_template("login.html"), 400

            if row is None:
                flash("invalid credentials", "error")
                return render_template("login.html"), 401

            session["user_id"] = row["id"]
            session["username"] = row["username"]
            session["is_admin"] = bool(row["is_admin"])
            flash(f"welcome, {row['username']}", "info")
            return redirect(url_for("notes"))
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        flash("logged out", "info")
        return redirect(url_for("index"))

    # -- notes --------------------------------------------------------------

    @app.get("/notes")
    def notes():
        if "user_id" not in session:
            return redirect(url_for("login"))
        db = get_db()
        rows = db.execute(
            "SELECT id, title, body, is_secret, created_at FROM notes "
            "WHERE owner_id = ? ORDER BY id DESC",
            (session["user_id"],),
        ).fetchall()
        return render_template("notes.html", notes=rows)

    @app.route("/notes/new", methods=["GET", "POST"])
    def notes_new():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if request.method == "POST":
            title = (request.form.get("title") or "").strip() or "untitled"
            body = request.form.get("body") or ""
            db = get_db()
            cur = db.execute(
                "INSERT INTO notes (owner_id, title, body, is_secret) "
                "VALUES (?, ?, ?, 0)",
                (session["user_id"], title, body),
            )
            db.commit()
            note_id = cur.lastrowid

            # Persist a copy under /app/data/notes so users can export them.
            note_path = NOTES_DIR / f"note_{note_id}.txt"
            note_path.write_text(f"{title}\n\n{body}\n")
            flash("note saved", "info")
            return redirect(url_for("notes"))
        return render_template("note_new.html")

    # -- search (parameterized, no vuln) ------------------------------------

    @app.get("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        rows = []
        if q:
            db = get_db()
            rows = db.execute(
                "SELECT n.id, n.title, n.body, u.username AS author "
                "FROM notes n JOIN users u ON u.id = n.owner_id "
                "WHERE n.is_secret = 0 AND (n.title LIKE ? OR n.body LIKE ?) "
                "ORDER BY n.id DESC LIMIT 50",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        return render_template("search.html", q=q, notes=rows)

    # -- export (path traversal vuln) ---------------------------------------

    @app.get("/export")
    def export_note():
        # VULN: path traversal. The user-supplied filename is concatenated
        # onto NOTES_DIR without any normalization, so payloads such as
        # ../flag.txt or ../../etc/passwd escape the intended directory.
        name = request.args.get("file") or ""
        if not name:
            return render_template("export.html", body=None, error=None)
        target = os.path.join(str(NOTES_DIR), name)
        logger.info("export request: %s -> %s", name, target)
        try:
            with open(target, "rb") as fh:
                payload = fh.read()
        except FileNotFoundError:
            return render_template(
                "export.html", body=None, error=f"not found: {name}"
            ), 404
        except IsADirectoryError:
            return render_template(
                "export.html", body=None, error=f"is a directory: {name}"
            ), 400
        except OSError as exc:
            return render_template(
                "export.html", body=None, error=f"read error: {exc}"
            ), 400
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - defensive
            text = payload.hex()
        return render_template("export.html", body=text, error=None)

    # -- diagnostics (command injection vuln) -------------------------------

    @app.route("/admin/diagnostics", methods=["GET", "POST"])
    def diagnostics():
        output = None
        host = ""
        if request.method == "POST":
            host = (request.form.get("host") or "").strip()
            if host:
                # VULN: command injection. The host argument is interpolated
                # into a shell command, so payloads such as
                #     127.0.0.1; cat /app/data/flag.txt
                # break out and execute arbitrary commands.
                cmd = f"ping -c 1 -W 1 {host}"
                logger.info("diagnostics cmd: %s", cmd)
                try:
                    proc = subprocess.run(  # noqa: S602 - intentionally vulnerable
                        cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    output = proc.stdout + proc.stderr
                except subprocess.TimeoutExpired:
                    output = "command timed out"
        return render_template("diagnostics.html", host=host, output=output)

    # -- gameserver hooks ---------------------------------------------------

    @app.post("/internal/plant")
    def plant_flag():
        """Allow the gameserver to rotate the planted flag for a new round.

        Authenticated by a shared secret to prevent attackers from
        overwriting their target flag. This endpoint is only meant to be
        called by the gameserver from inside the ctf-network.
        """
        token = request.headers.get("X-Plant-Token") or ""
        expected = os.environ.get("PLANT_TOKEN", SECRET_KEY)
        if not secrets.compare_digest(token, expected):
            abort(403)
        new_flag = (request.json or {}).get("flag")
        if not new_flag or not isinstance(new_flag, str):
            return {"error": "missing flag"}, 400
        FLAG_FILE.write_text(new_flag.strip() + "\n")

        db = get_db()
        admin_id = db.execute(
            "SELECT id FROM users WHERE username = 'admin'"
        ).fetchone()["id"]
        db.execute(
            "UPDATE notes SET body = ? WHERE owner_id = ? AND is_secret = 1",
            (f"The current flag is: {new_flag.strip()}", admin_id),
        )
        db.commit()
        return {"status": "planted"}


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=False)  # noqa: S104
