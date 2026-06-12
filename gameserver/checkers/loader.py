from __future__ import annotations

import importlib.util
from pathlib import Path

from checkers.contract import CheckerMetadata, CheckerPlugin


def load_checker(path: str | Path) -> CheckerPlugin:
    checker_path = Path(path).resolve()
    if not checker_path.is_file():
        raise FileNotFoundError(f"checker module not found: {checker_path}")

    module_name = f"sandcastle_service_checker_{abs(hash(str(checker_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, checker_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load checker module: {checker_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker = getattr(module, "CHECKER", None)
    if checker is None:
        raise ImportError(f"{checker_path} must export CHECKER")
    if not isinstance(checker, CheckerPlugin):
        raise TypeError(f"{checker_path} CHECKER does not implement CheckerPlugin")
    if not isinstance(checker.metadata, CheckerMetadata):
        raise TypeError(f"{checker_path} CHECKER metadata is invalid")
    return checker
