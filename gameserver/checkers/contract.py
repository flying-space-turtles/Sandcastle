from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable


class Transport(str, enum.Enum):
    HTTP = "HTTP"
    TCP = "TCP"


class CheckerOperation(str, enum.Enum):
    PUT = "PUT"
    GET = "GET"
    CHECK = "CHECK"


class CheckerStatus(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"
    MUMBLE = "MUMBLE"
    CORRUPT = "CORRUPT"


@dataclass(frozen=True)
class CheckerMetadata:
    name: str
    service_name: str
    version: str
    transport: Transport
    default_port: int
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.name or not self.service_name or not self.version:
            raise ValueError("checker metadata names and version must be non-empty")
        if not isinstance(self.transport, Transport):
            raise TypeError("checker transport must be a Transport value")
        if not 1 <= self.default_port <= 65535:
            raise ValueError("checker default_port must be between 1 and 65535")
        if self.timeout_seconds <= 0:
            raise ValueError("checker timeout_seconds must be positive")


@dataclass(frozen=True)
class ServiceTarget:
    team_id: int
    service_id: int
    service_name: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.team_id <= 0 or self.service_id <= 0:
            raise ValueError("team_id and service_id must be positive")
        if not self.service_name or not self.host:
            raise ValueError("service_name and host must be non-empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("target port must be between 1 and 65535")


@dataclass(frozen=True)
class CheckerCredentials:
    team_id: int
    service_name: str
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.team_id <= 0 or not self.service_name:
            raise ValueError("checker credential scope must identify a team and service")
        copied_values = dict(self.values)
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in copied_values.items()
        ):
            raise TypeError("checker credential keys and values must be strings")
        object.__setattr__(self, "values", copied_values)

    def require(self, key: str) -> str:
        value = self.values.get(key, "")
        if not value:
            raise ValueError(f"checker credential '{key}' is missing")
        return value

    def validate_scope(self, target: ServiceTarget) -> None:
        if self.team_id != target.team_id or self.service_name != target.service_name:
            raise ValueError("checker credentials do not match the team/service target")


@dataclass(frozen=True)
class OperationContext:
    target: ServiceTarget
    credentials: CheckerCredentials
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("operation timeout_seconds must be positive")


@dataclass(frozen=True)
class PutRequest:
    operation: ClassVar[CheckerOperation] = CheckerOperation.PUT
    context: OperationContext
    flag: str

    def __post_init__(self) -> None:
        if not self.flag:
            raise ValueError("PUT requires a non-empty flag")


@dataclass(frozen=True)
class GetRequest:
    operation: ClassVar[CheckerOperation] = CheckerOperation.GET
    context: OperationContext
    flag: str
    state: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.flag:
            raise ValueError("GET requires a non-empty flag")


@dataclass(frozen=True)
class CheckRequest:
    operation: ClassVar[CheckerOperation] = CheckerOperation.CHECK
    context: OperationContext


CheckerRequest = PutRequest | GetRequest | CheckRequest


@dataclass(frozen=True)
class CheckerOutcome:
    status: CheckerStatus
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckerStatus):
            raise TypeError("checker outcome status must be a CheckerStatus")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("checker outcome message must be non-empty")
        copied_data = dict(self.data)
        json.dumps(copied_data, allow_nan=False)
        object.__setattr__(self, "data", copied_data)


@dataclass(frozen=True)
class CheckerResult:
    plugin_name: str
    plugin_version: str
    operation: CheckerOperation
    status: CheckerStatus
    message: str
    duration_ms: int
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plugin_name or not self.plugin_version:
            raise ValueError("checker result must identify the plugin")
        if not isinstance(self.operation, CheckerOperation):
            raise TypeError("checker result operation must be a CheckerOperation")
        if not isinstance(self.status, CheckerStatus):
            raise TypeError("checker result status must be a CheckerStatus")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("checker result message must be non-empty")
        if self.duration_ms < 0:
            raise ValueError("checker result duration_ms must be non-negative")
        copied_data = dict(self.data)
        json.dumps(copied_data, allow_nan=False)
        object.__setattr__(self, "data", copied_data)


@runtime_checkable
class CheckerPlugin(Protocol):
    metadata: CheckerMetadata

    def put(self, request: PutRequest) -> CheckerOutcome:
        ...

    def get(self, request: GetRequest) -> CheckerOutcome:
        ...

    def check(self, request: CheckRequest) -> CheckerOutcome:
        ...
