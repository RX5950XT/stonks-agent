"""Minimal standard-library child process for one factor expression."""

from __future__ import annotations

import builtins
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

_BUILTIN_UNIVERSE = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "range",
        "round",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)


def restricted_builtins(names: tuple[str, ...]) -> Mapping[str, object]:
    if not names or set(names) - _BUILTIN_UNIVERSE:
        raise ValueError("invalid builtin allowlist")
    return MappingProxyType({name: getattr(builtins, name) for name in names})


def freeze_value(value: object, *, depth: int = 0) -> object:
    if depth > 16:
        raise ValueError("candidate input nesting is too deep")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("candidate input keys must be strings")
        return MappingProxyType(
            {key: freeze_value(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_value(item, depth=depth + 1) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("candidate input contains an unsupported value")


def execute_candidate(source: str, payload: object) -> object:
    if not isinstance(payload, dict) or set(payload) != {"allowed_calls", "rows"}:
        raise ValueError("candidate payload shape is invalid")
    raw_calls = payload["allowed_calls"]
    if not isinstance(raw_calls, list) or any(
        not isinstance(value, str) for value in raw_calls
    ):
        raise ValueError("candidate builtin allowlist is invalid")
    safe_globals: dict[str, Any] = {
        "__builtins__": restricted_builtins(tuple(raw_calls))
    }
    local_values: dict[str, Any] = {}
    code = compile(source, "<candidate>", "exec", dont_inherit=True, optimize=2)
    exec(code, safe_globals, local_values)
    compute = local_values.get("compute")
    if not callable(compute):
        raise ValueError("candidate entrypoint is missing")
    result = compute(freeze_value(payload["rows"]))
    if not isinstance(result, list):
        raise ValueError("candidate result must be a list")
    return result


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv
    if len(arguments) != 4:
        return 64
    source_path, input_path, output_path = map(Path, arguments[1:])
    try:
        source = source_path.read_text(encoding="utf-8")
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        result = execute_candidate(source, payload)
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_exclusive(output_path, encoded)
    except MemoryError:
        return 70
    except BaseException:
        return 71
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
