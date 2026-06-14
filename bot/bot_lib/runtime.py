from __future__ import annotations

import http.client
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import BotConfig

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

DOCKER_SOCK = "/var/run/docker.sock"
SERVICE_CONTROL_PORT = int(os.environ.get("SERVICE_CONTROL_PORT", "7979"))


def ok(msg: str) -> None:
    print(f"{GREEN}[+]{RESET} {msg}")


def info(msg: str) -> None:
    print(f"{CYAN}[*]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[!]{RESET} {msg}")


def err(msg: str) -> None:
    print(f"{RED}[-]{RESET} {msg}")


def detect_my_team() -> Optional[int]:
    env = socket.gethostname()
    match = re.match(r"team(\d+)", env)
    if match:
        return int(match.group(1))
    return None


def _service_control_host(my_team: int) -> str:
    """Return the ctf-network IP of teamN-vuln (10.10.N.3)."""
    return f"10.10.{my_team}.3"


def discover_capabilities(my_team: Optional[int]) -> frozenset:
    """Probe available capabilities at startup. Returns a frozenset of token strings."""
    caps: set[str] = {"network.attack", "network.submit"}
    if os.path.exists(DOCKER_SOCK):
        caps.add("docker.socket")
    if my_team is not None:
        try:
            url = f"http://{_service_control_host(my_team)}:{SERVICE_CONTROL_PORT}/ping"
            urllib.request.urlopen(url, timeout=2)
            caps.add("service.control.local")
        except Exception:
            pass
    return frozenset(caps)


def call_service_control(host: str, method: str, path: str) -> dict:
    """Call the team-local service-control API. Raises on error."""
    url = f"http://{host}:{SERVICE_CONTROL_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    if method == "POST":
        req.data = b""
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


@dataclass
class BotContext:
    config: BotConfig
    num_teams: int
    my_team: int | None
    hostname: str = field(default_factory=socket.gethostname)
    _flag_re_cache: re.Pattern | None = field(default=None, init=False, repr=False)
    _submitted_flags: set[str] = field(default_factory=set, init=False, repr=False)
    event_file: str = field(default_factory=lambda: os.environ.get("BOT_EVENT_FILE", ""))
    # Pass capabilities=frozenset() in unit tests to skip the network probe.
    capabilities: frozenset | None = field(default=None)

    def __post_init__(self) -> None:
        if self.capabilities is None:
            object.__setattr__(self, "capabilities", discover_capabilities(self.my_team))

    def flag_re(self) -> re.Pattern:
        if self._flag_re_cache is None:
            self._flag_re_cache = re.compile(self.config.flag_re)
        return self._flag_re_cache

    def target_ip(self, team_id: int) -> str:
        return self.config.ip_pattern.format(team=team_id)

    def service_url(self, team_id: int) -> str:
        return f"http://{self.target_ip(team_id)}:{self.config.service_port}"

    def emit(self, event_type: str, **details: object) -> None:
        if not self.event_file:
            return
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "deployment_id": self.config.deployment_id,
            "team_id": self.my_team,
            **details,
        }
        try:
            path = Path(self.event_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError:
            pass

    @staticmethod
    def flag_fingerprint(flag: str) -> str:
        return hashlib.sha256(flag.encode("utf-8")).hexdigest()[:12]

    def submit_flag(self, flag: str, target_team: int, action_id: str) -> dict[str, object]:
        fingerprint = self.flag_fingerprint(flag)
        if flag in self._submitted_flags:
            outcome = {"code": "LOCAL_DUPLICATE", "accepted": False}
            self.emit(
                "submission.completed",
                target_team=target_team,
                action_id=action_id,
                flag_fingerprint=fingerprint,
                **outcome,
            )
            return outcome

        self._submitted_flags.add(flag)
        if (
            not self.config.gameserver_url
            or not self.config.submission_token
            or self.my_team is None
        ):
            outcome = {"code": "NOT_CONFIGURED", "accepted": False}
            self.emit(
                "submission.completed",
                target_team=target_team,
                action_id=action_id,
                flag_fingerprint=fingerprint,
                **outcome,
            )
            return outcome

        url = f"{self.config.gameserver_url.rstrip('/')}/api/flags/submit"
        payload = json.dumps({"team_id": self.my_team, "flag": flag}).encode()
        self.emit(
            "submission.started",
            target_team=target_team,
            action_id=action_id,
            flag_fingerprint=fingerprint,
        )

        for attempt in range(2):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.config.submission_token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    body = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
                    outcome = {
                        "code": str(body.get("code", "ACCEPTED")),
                        "accepted": bool(body.get("accepted", response.status == 201)),
                        "http_status": response.status,
                    }
                    break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    body = {}
                outcome = {
                    "code": str(body.get("code", f"HTTP_{exc.code}")),
                    "accepted": False,
                    "http_status": exc.code,
                }
                if exc.code == 429 and attempt == 0:
                    retry_after = exc.headers.get("Retry-After", "1")
                    time.sleep(min(2, int(retry_after) if retry_after.isdigit() else 1))
                    continue
                break
            except (urllib.error.URLError, OSError) as exc:
                outcome = {
                    "code": "TRANSPORT_ERROR",
                    "accepted": False,
                    "message": str(exc)[:160],
                }
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                break

        self.emit(
            "submission.completed",
            target_team=target_team,
            action_id=action_id,
            flag_fingerprint=fingerprint,
            **outcome,
        )
        return outcome

    def get(self, url: str, opener=None) -> str | None:
        try:
            fn = opener.open if opener else urllib.request.urlopen
            with fn(url, timeout=self.config.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError):
            return None

    def post(self, url: str, data: dict[str, str], opener=None) -> str | None:
        payload = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=payload)
        try:
            fn = opener.open if opener else urllib.request.urlopen
            with fn(req, timeout=self.config.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError):
            return None

    def post_json(
        self,
        url: str,
        body: dict[str, object],
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        for key, value in (extra_headers or {}).items():
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str) -> None:
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._sock_path)
        self.sock = sock


def docker_get(path: str):
    try:
        conn = UnixSocketHTTPConnection(DOCKER_SOCK)
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read())
    except Exception:
        pass
    return None


def docker_post(path: str) -> bool:
    try:
        conn = UnixSocketHTTPConnection(DOCKER_SOCK)
        conn.request("POST", path, headers={"Host": "localhost", "Content-Length": "0"})
        resp = conn.getresponse()
        return 200 <= resp.status < 300
    except Exception:
        return False


def ping_team(ctx: BotContext, team_id: int) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", ctx.target_ip(team_id)],
        capture_output=True,
    )
    return result.returncode == 0


def ping_all(ctx: BotContext) -> None:
    info(f"Ping sweep - {ctx.num_teams} teams")
    for team_id in range(1, ctx.num_teams + 1):
        ip = ctx.target_ip(team_id)
        alive = ping_team(ctx, team_id)
        status = f"{GREEN}UP{RESET}" if alive else f"{RED}DOWN{RESET}"
        print(f"  team{team_id:>2}  {ip}  {status}")
