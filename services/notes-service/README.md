# Notes — example vulnerable service

A tiny REST notes app. Each user is identified by a bearer token. They can
create notes, read a specific note, or list notes.

## Endpoints

| Method | Path                    | Description                                                  |
| ------ | ----------------------- | ------------------------------------------------------------ |
| GET    | `/`                     | Service banner                                               |
| GET    | `/health`               | Liveness probe                                               |
| POST   | `/api/register`         | Register a new user; returns a bearer token                  |
| POST   | `/api/notes`            | Create a note (Authorization: Bearer)                        |
| GET    | `/api/note/<id>`        | Read a note by ID — *does not check ownership* (intended)    |
| GET    | `/api/notes`            | List **all** notes in the system — IDOR                      |
| POST   | `/api/admin/reset`      | Wipe all data; requires `X-Admin-Token` header               |

## Vulnerability

`GET /api/notes` returns every note in the database, ignoring the caller's
identity (no Authorization header required). An attacker uses this to scrape
flags that the gameserver planted in another team's database.

A defending team can patch the bug by either filtering `/api/notes` to the
authenticated user, or by removing the endpoint outright while keeping
`POST /api/notes` and `GET /api/note/<id>` working (which the SLA checker
needs).
