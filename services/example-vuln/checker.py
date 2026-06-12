from __future__ import annotations

import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from checkers.contract import (
    CheckRequest,
    CheckerMetadata,
    CheckerOutcome,
    CheckerStatus,
    GetRequest,
    PutRequest,
    Transport,
)


class _HttpSession:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, path: str) -> tuple[int, str]:
        return self._open(urllib.request.Request(f"{self.base_url}{path}"))

    def post_form(self, path: str, values: dict[str, str]) -> tuple[int, str]:
        data = urllib.parse.urlencode(values).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._open(request)

    def post_json(
        self,
        path: str,
        values: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[int, str]:
        data = json.dumps(values).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json", **headers},
        )
        return self._open(request)

    def _open(self, request: urllib.request.Request) -> tuple[int, str]:
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")


class TurtleNotesChecker:
    metadata = CheckerMetadata(
        name="turtlenotes",
        service_name="example-vuln",
        version="1.0.0",
        transport=Transport.HTTP,
        default_port=8080,
        timeout_seconds=5.0,
    )

    def __init__(
        self,
        session_factory: Callable[[str, float], _HttpSession] = _HttpSession,
    ) -> None:
        self._session_factory = session_factory

    def put(self, request: PutRequest) -> CheckerOutcome:
        session = self._session(request)
        token = request.context.credentials.require("plant_token")
        try:
            status, body = session.post_json(
                "/internal/plant",
                {"flag": request.flag},
                {"X-Plant-Token": token},
            )
        except urllib.error.HTTPError as exc:
            return self._http_mumble("flag plant", exc)

        if status != 200:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"flag plant returned HTTP {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return CheckerOutcome(CheckerStatus.MUMBLE, "flag plant returned invalid JSON")
        if payload.get("status") != "planted":
            return CheckerOutcome(CheckerStatus.MUMBLE, "flag plant response was malformed")
        return CheckerOutcome(
            CheckerStatus.UP,
            "flag planted through the authenticated service endpoint",
            {"retrieval": "checker-account"},
        )

    def get(self, request: GetRequest) -> CheckerOutcome:
        session = self._session(request)
        try:
            login = self._login(session, request)
            if login is not None:
                return login
            status, notes = session.get("/notes")
        except urllib.error.HTTPError as exc:
            return self._http_mumble("flag retrieval", exc)

        if status != 200:
            return CheckerOutcome(CheckerStatus.MUMBLE, f"notes returned HTTP {status}")
        if request.flag not in notes:
            return CheckerOutcome(
                CheckerStatus.CORRUPT,
                "previously planted flag is missing from the checker account",
            )
        return CheckerOutcome(
            CheckerStatus.UP,
            "previously planted flag is retrievable through an authenticated login",
            {"retrieval": "checker-account"},
        )

    def check(self, request: CheckRequest) -> CheckerOutcome:
        session = self._session(request)
        marker = f"checker-{secrets.token_hex(8)}"
        try:
            health_status, health_body = session.get("/health")
            if health_status != 200:
                return CheckerOutcome(
                    CheckerStatus.MUMBLE,
                    f"health returned HTTP {health_status}",
                )
            try:
                health = json.loads(health_body)
            except json.JSONDecodeError:
                return CheckerOutcome(CheckerStatus.MUMBLE, "health returned invalid JSON")
            if health.get("status") != "ok":
                return CheckerOutcome(CheckerStatus.MUMBLE, "health payload was malformed")

            login = self._login(session, request)
            if login is not None:
                return login
            create_status, notes = session.post_form(
                "/notes/new",
                {"title": marker, "body": f"benign checker note {marker}"},
            )
        except urllib.error.HTTPError as exc:
            return self._http_mumble("benign workflow", exc)

        if create_status != 200:
            return CheckerOutcome(
                CheckerStatus.MUMBLE,
                f"note creation returned HTTP {create_status}",
            )
        if marker not in notes:
            return CheckerOutcome(
                CheckerStatus.MUMBLE,
                "created note was not visible after the redirect",
            )
        return CheckerOutcome(
            CheckerStatus.UP,
            "health, login, note creation, and note retrieval succeeded",
            {"checks": ["health", "login", "create-note", "read-notes"]},
        )

    def _session(self, request: PutRequest | GetRequest | CheckRequest) -> _HttpSession:
        target = request.context.target
        return self._session_factory(
            f"http://{target.host}:{target.port}",
            request.context.timeout_seconds,
        )

    def _login(
        self,
        session: _HttpSession,
        request: GetRequest | CheckRequest,
    ) -> CheckerOutcome | None:
        credentials = request.context.credentials
        status, body = session.post_form(
            "/login",
            {
                "username": credentials.require("username"),
                "password": credentials.require("password"),
            },
        )
        if status != 200 or "'s notes" not in body:
            return CheckerOutcome(CheckerStatus.MUMBLE, "checker account login failed")
        return None

    @staticmethod
    def _http_mumble(action: str, error: urllib.error.HTTPError) -> CheckerOutcome:
        return CheckerOutcome(
            CheckerStatus.MUMBLE,
            f"{action} returned HTTP {error.code}",
            {"http_status": error.code},
        )


CHECKER = TurtleNotesChecker()
