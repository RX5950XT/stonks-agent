"""Export deterministic JSON Schema snapshots for stonks-contracts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_SRC = ROOT / "packages" / "contracts" / "src"
DEFAULT_OUTPUT = ROOT / "schemas" / "v1"
sys.path.insert(0, str(CONTRACTS_SRC))

from stonks_contracts import SCHEMA_MODELS  # noqa: E402
from stonks_contracts.common import ContractModel  # noqa: E402


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def render_schema(model: type[ContractModel]) -> str:
    """Render one model schema with stable key ordering and final newline."""
    schema = model.model_json_schema(mode="serialization")
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def rendered_schemas(
    models: Iterable[type[ContractModel]] = SCHEMA_MODELS,
) -> dict[str, str]:
    """Return deterministic filename-to-content mappings."""
    return {
        _snake_case(model.__name__) + ".json": render_schema(model) for model in models
    }


def export_schemas(output_dir: Path = DEFAULT_OUTPUT) -> tuple[Path, ...]:
    """Write the complete v1 schema set, removing stale JSON snapshots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = rendered_schemas()
    for stale in output_dir.glob("*.json"):
        if stale.name not in rendered:
            stale.unlink()
    written: list[Path] = []
    for name, content in sorted(rendered.items()):
        path = output_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return tuple(written)


def check_schemas(output_dir: Path = DEFAULT_OUTPUT) -> tuple[str, ...]:
    """Return snapshot differences without mutating the schema tree."""
    rendered = rendered_schemas()
    actual_names = {path.name for path in output_dir.glob("*.json")}
    differences = [
        f"unexpected schema: {name}" for name in sorted(actual_names - rendered.keys())
    ]
    for name, expected in sorted(rendered.items()):
        path = output_dir / name
        if not path.exists():
            differences.append(f"missing schema: {name}")
        elif path.read_text(encoding="utf-8") != expected:
            differences.append(f"changed schema: {name}")
    return tuple(differences)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if snapshots are stale"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.check:
        differences = check_schemas(args.output)
        if differences:
            print("\n".join(differences))
            return 1
        print(f"schemas current: {args.output}")
        return 0
    written = export_schemas(args.output)
    print(f"exported {len(written)} schemas to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
