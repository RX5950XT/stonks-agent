"""The exact markets whose calendar and provider route were both verified."""

from __future__ import annotations

from types import MappingProxyType

from stonks_agent.application.market_freshness import MarketRegionFreshnessPolicy
from stonks_agent.composition.tw_market import xtai_2026_freshness_policy
from stonks_agent.composition.us_market import xnas_2026_freshness_policy

# A market belongs here only when it has a versioned exchange calendar AND an
# actually verified provider route. Anything else assesses as unknown instead of
# borrowing another exchange's sessions.
VERIFIED_MARKETS = frozenset({"US", "TW"})


def verified_market_freshness_policy() -> MarketRegionFreshnessPolicy:
    return MarketRegionFreshnessPolicy(
        calendars=MappingProxyType(
            {
                "US": xnas_2026_freshness_policy(),
                "TW": xtai_2026_freshness_policy(),
            }
        )
    )
