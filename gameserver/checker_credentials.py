#!/usr/bin/env python3
from __future__ import annotations

import argparse

from checkers.credentials import derive_checker_credentials


def main() -> int:
    parser = argparse.ArgumentParser(description="derive scoped checker credentials")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--team", required=True, type=int)
    parser.add_argument("--service", required=True)
    args = parser.parse_args()

    credentials = derive_checker_credentials(args.secret, args.team, args.service)
    print(
        credentials.require("username"),
        credentials.require("password"),
        credentials.require("plant_token"),
        sep="\t",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
