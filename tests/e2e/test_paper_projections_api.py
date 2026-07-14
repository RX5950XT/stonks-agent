from __future__ import annotations

from application.projections.test_queries import NOW, Factory, _valuation
from fastapi.testclient import TestClient

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.entrypoints.api.routes.projections import (
    create_paper_projection_app,
)

TOKEN = "paper-projection-test-token-that-is-long-enough"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def _app(factory: Factory):  # type: ignore[no-untyped-def]
    return create_paper_projection_app(
        factory,
        LocalTokenAuthenticator(
            token=TOKEN,
            subject="viewer:one",
            roles=frozenset({Role.VIEWER}),
            allowed_hosts=frozenset({"testclient"}),
        ),
        clock=lambda: NOW,
    )


def test_projection_api_exposes_uniform_read_only_portfolio_nav_and_risk() -> None:
    ledger, _ = _valuation()
    factory = Factory(ledger)
    client = TestClient(_app(factory))

    portfolio = client.get(
        "/v1/paper/accounts/paper-monitoring/portfolio", headers=AUTHORIZATION
    )
    nav = client.get("/v1/paper/accounts/paper-monitoring/nav", headers=AUTHORIZATION)
    risk = client.get("/v1/paper/accounts/paper-monitoring/risk", headers=AUTHORIZATION)

    assert portfolio.status_code == nav.status_code == risk.status_code == 200
    assert portfolio.json()["data"]["projection_hash"]
    assert nav.json()["data"]["valuation_hash"]
    assert risk.json()["data"]["decision_hash"]
    assert factory.uow.commits == 0


def test_projection_api_defaults_to_deny_all_and_validates_account_path() -> None:
    ledger, _ = _valuation()
    factory = Factory(ledger)
    denied = TestClient(create_paper_projection_app(factory, clock=lambda: NOW))
    client = TestClient(_app(factory))

    unauthorized = denied.get("/v1/paper/accounts/paper-monitoring/nav")
    invalid = client.get(
        "/v1/paper/accounts/invalid%20account/nav", headers=AUTHORIZATION
    )

    assert unauthorized.status_code == 401
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_input"
