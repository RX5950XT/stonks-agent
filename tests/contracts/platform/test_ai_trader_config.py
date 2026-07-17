from __future__ import annotations

import json
from pathlib import Path

import yaml

from stonks_agent.adapters.platform import AI_TRADER_ENDPOINT_TEMPLATES

ROOT = Path(__file__).parents[3]


def test_optional_config_is_default_off_scoped_and_non_retrying() -> None:
    config = yaml.safe_load(
        (ROOT / "config" / "platforms" / "ai_trader.yaml").read_text(encoding="utf-8")
    )

    assert config["enabled"] is False
    assert config["origin"] == "https://api.ai4trade.ai"
    assert config["access_token_ref"] == "ai_trader_access_token"
    assert config["http"]["follow_redirects"] is False
    assert config["http"]["automatic_post_retries"] == 0
    assert (
        frozenset(config["allowed_endpoint_templates"]) == AI_TRADER_ENDPOINT_TEMPLATES
    )
    assert config["capabilities"]["canonical_order_submission"] is False
    assert config["capabilities"]["copy_trading"] is False


def test_runtime_cassette_manifest_is_snapshot_bound_and_live_unverified() -> None:
    manifest = json.loads(
        (
            ROOT / "tests" / "fixtures" / "platform" / "ai_trader" / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["upstream_snapshot"] == "d03ff6c056b32ced735adf7c19ed8175adb1c8df"
    assert manifest["source_mode"] == "clean-room-runtime-shape"
    assert manifest["live_openapi_verified"] is False
    assert frozenset(manifest["cassettes"].values()) <= AI_TRADER_ENDPOINT_TEMPLATES
