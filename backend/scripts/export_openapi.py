#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema to docs/api/openapi.json (Wave 0, U4).

The committed file is the API-contract snapshot gated by the "api-contract"
CI job: CI regenerates it from HEAD and runs oasdiff against the committed
version, failing on breaking changes (ERR severity). After changing any
endpoint or response model:

    cd backend && .venv/bin/python scripts/export_openapi.py
    git add docs/api/openapi.json   # review the diff — it IS the contract

Usage:
    python scripts/export_openapi.py [output_path]
"""

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app


def export_openapi(output_path: str | None = None) -> None:
    """Dump app.openapi() sorted and indented so diffs stay readable."""
    path = (
        Path(output_path)
        if output_path is not None
        else BACKEND_ROOT.parent / "docs" / "api" / "openapi.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    schema = app.openapi()
    with path.open("w") as f:
        json.dump(schema, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"OpenAPI schema exported to {path}")
    print(f"  Paths: {len(schema.get('paths', {}))}")
    print(f"  Components: {len(schema.get('components', {}).get('schemas', {}))}")


if __name__ == "__main__":
    export_openapi(sys.argv[1] if len(sys.argv) > 1 else None)
