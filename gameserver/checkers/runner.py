from __future__ import annotations

import concurrent.futures
import socket
import sqlite3
import time
import urllib.error
from collections.abc import Callable

import db
from checkers.contract import (
    CheckerOperation,
    CheckerOutcome,
    CheckerPlugin,
    CheckerRequest,
    CheckerResult,
    CheckerStatus,
)


class CheckerRunner:
    def run(
        self,
        conn: sqlite3.Connection,
        plugin: CheckerPlugin,
        request: CheckerRequest,
        round_number: int,
        match_id: int = 1,
    ) -> CheckerResult:
        if round_number < 0:
            raise ValueError("round_number must be non-negative")

        result = self.execute(plugin, request)
        db.persist_checker_result(
            conn,
            target=request.context.target,
            round_number=round_number,
            result=result,
            match_id=match_id,
        )
        return result

    def execute(
        self,
        plugin: CheckerPlugin,
        request: CheckerRequest,
    ) -> CheckerResult:
        """Execute without persistence so callers can serialize database writes."""
        started = time.monotonic()
        operation = request.operation
        outcome = self._execute(plugin, request)
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        result = CheckerResult(
            plugin_name=plugin.metadata.name,
            plugin_version=plugin.metadata.version,
            operation=operation,
            status=outcome.status,
            message=outcome.message,
            duration_ms=duration_ms,
            data=outcome.data,
        )
        return result

    def _execute(
        self,
        plugin: CheckerPlugin,
        request: CheckerRequest,
    ) -> CheckerOutcome:
        try:
            request.context.credentials.validate_scope(request.context.target)
            if plugin.metadata.service_name != request.context.target.service_name:
                raise ValueError("checker plugin does not match the target service")

            operation_method: Callable[[CheckerRequest], CheckerOutcome]
            if request.operation is CheckerOperation.PUT:
                operation_method = plugin.put
            elif request.operation is CheckerOperation.GET:
                operation_method = plugin.get
            else:
                operation_method = plugin.check

            timeout = request.context.timeout_seconds
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(operation_method, request)
            try:
                outcome = future.result(timeout=timeout)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if not isinstance(outcome, CheckerOutcome):
                raise TypeError("checker operation returned an invalid outcome")
            return outcome
        except concurrent.futures.TimeoutError:
            return CheckerOutcome(
                CheckerStatus.DOWN,
                "checker operation timed out",
                {"failure": "timeout"},
            )
        except (socket.timeout, socket.gaierror, TimeoutError, ConnectionError) as exc:
            return CheckerOutcome(
                CheckerStatus.DOWN,
                f"service unavailable: {type(exc).__name__}",
                {"failure": "network"},
            )
        except urllib.error.HTTPError as exc:
            return CheckerOutcome(
                CheckerStatus.MUMBLE,
                f"unexpected HTTP status {exc.code}",
                {"failure": "protocol", "http_status": exc.code},
            )
        except urllib.error.URLError:
            return CheckerOutcome(
                CheckerStatus.DOWN,
                "service unavailable: URL error",
                {"failure": "network"},
            )
        except Exception as exc:  # noqa: BLE001 - contract boundary
            return CheckerOutcome(
                CheckerStatus.MUMBLE,
                f"checker exception: {type(exc).__name__}",
                {"failure": "exception"},
            )
