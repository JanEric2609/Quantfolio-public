"""Schemathesis API-conformance fuzz over the live OpenAPI schema (Wave 0, U5).

OPT-IN and never part of the default suite: the whole module skips unless
SCHEMATHESIS=1, mirroring the TAXVER_GOLDEN_CAPTURE gating pattern in
tests/test_tax_verification_contracts.py. CI runs it only in the optional,
non-blocking "api-fuzz-experimental" job.

Run locally:

    cd backend && DATABASE_URL="sqlite:///:memory:" SCHEMATHESIS=1 \
        .venv/bin/pytest tests/test_api_contract_schemathesis.py -q

Scope: unauthenticated conformance — every operation must answer generated
input without a 5xx. Auth-protected operations fail fast with 401/403/422,
which is conformant behaviour. Endpoints that would reach external
providers or long-running jobs without auth are excluded explicitly below,
each with the reason observed on the first run.
"""

import os

import pytest

if os.environ.get("SCHEMATHESIS") != "1":
    pytest.skip("conformance fuzz runs only with SCHEMATHESIS=1", allow_module_level=True)

import schemathesis
from hypothesis import HealthCheck, settings

from app.main import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)

# Explicit check list, deliberately narrower than the default set:
# - "unsupported method" dropped: Starlette's generated 405 responses omit
#   the RFC 9110 `Allow` header — a framework-level nit we do not patch in
#   production code.
# - "status code conformance" dropped: routers legitimately answer 401/403/
#   404/409/429 without documenting every status in their response_model;
#   widening all of those models is out of Wave 0 scope.
# What remains is the core fuzz value: no generated input may crash an
# operation (5xx) or return a malformed content type.
CHECKS = [
    schemathesis.checks.not_a_server_error,
    schemathesis.checks.content_type_conformance,
]

# path -> reason. Populated from observed first-run flakes; keep entries
# honest: each must name what the endpoint does that makes generated input
# unsafe or slow (external network, heavy compute), not merely inconvenient.
EXCLUDED_PATHS = {}


@schema.parametrize()
@settings(max_examples=20, deadline=None, suppress_health_check=list(HealthCheck))
def test_api_conformance(case):
    if case.operation.path in EXCLUDED_PATHS:
        pytest.skip(f"excluded: {EXCLUDED_PATHS[case.operation.path]}")
    case.call_and_validate(checks=CHECKS)
