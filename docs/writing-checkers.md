# Writing Service Checkers

Every service template must contain a `checker.py` next to its `Dockerfile` and
export a `CHECKER` object. Reference attacks stay under the service's
`exploits/` directory; checkers must use organizer endpoints or legitimate user
workflows rather than depend on a vulnerability.

## Contract

Import the typed API from `gameserver/checkers/contract.py`:

```python
from checkers.contract import CheckerMetadata, Transport


class MyChecker:
    metadata = CheckerMetadata(
        name="my-service",
        service_name="my-service",
        version="1.0.0",
        transport=Transport.TCP,
        default_port=31337,
        timeout_seconds=5.0,
    )

    def put(self, request):
        ...

    def get(self, request):
        ...

    def check(self, request):
        ...


CHECKER = MyChecker()
```

`Transport.HTTP` and `Transport.TCP` describe the service without changing the
operation contract. Each operation receives a `ServiceTarget`, scoped
`CheckerCredentials`, and a timeout:

- `put`: plant the supplied flag and return any non-secret retrieval state.
- `get`: prove that the supplied, previously planted flag is still retrievable.
- `check`: execute benign availability and functionality checks.

Return `CheckerOutcome` with one of these statuses:

| Status | Meaning |
|---|---|
| `UP` | The requested operation completed correctly. |
| `DOWN` | The service could not be reached before the deadline. |
| `MUMBLE` | The service responded but its protocol or behavior was wrong. |
| `CORRUPT` | GET reached the service but the expected flag was missing or changed. |

Plugins should convert service-level protocol failures, such as unexpected HTTP
responses, into `MUMBLE`. The runner deterministically converts deadline and
network failures to `DOWN`, and other uncaught exceptions to `MUMBLE`.

## Credentials

Credentials are derived independently for every `(team_id, service_name)` from
`ARENA_CHECKER_SECRET`. A plugin reads named values with
`request.context.credentials.require(...)` and must not persist secrets in its
result data. The runner rejects credentials whose scope does not match the
target before calling the plugin.

The bundled derivation provides `username`, `password`, and `plant_token`.
Services may define a different credential set as long as it remains scoped.
Generated TurtleNotes Compose files receive only that team's derived values;
the gameserver receives the master key.

## Adding A Service

1. Add `services/<service>/checker.py` and export `CHECKER`.
2. Make `metadata.service_name` match the template directory basename.
3. Provide bounded PUT, GET, and CHECK implementations using the request
   timeout for every network operation.
4. Keep exploit scripts in `services/<service>/exploits/`.
5. Add unit tests for `UP`, `DOWN`, `MUMBLE`, and `CORRUPT`, plus a real service
   workflow test where practical.
6. Copy the trusted checker module into the gameserver image in
   `gameserver/Dockerfile`.

Load a service-owned module with:

```python
from checkers.loader import load_checker

checker = load_checker("services/my-service/checker.py")
```

The runner stores structured PUT, GET, and CHECK records independently for each
team, service, and round. Re-running one operation replaces only that
operation's record.
