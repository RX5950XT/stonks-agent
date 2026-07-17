from __future__ import annotations

import inspect

import pytest

from stonks_agent.adapters.llm.anthropic import AnthropicAdapter
from stonks_agent.adapters.llm.openai_compatible import OpenAICompatibleAdapter
from stonks_agent.adapters.market_data.financial_datasets import (
    FinancialDatasetsAdapter,
)
from stonks_agent.adapters.platform.ai_trader import AiTraderHttpAdapter


@pytest.mark.parametrize(
    "adapter",
    [
        OpenAICompatibleAdapter,
        AnthropicAdapter,
        FinancialDatasetsAdapter,
        AiTraderHttpAdapter,
    ],
)
def test_outbound_adapters_cannot_be_constructed_with_raw_credentials(
    adapter: type[object],
) -> None:
    parameters = inspect.signature(adapter).parameters
    slots = set(getattr(adapter, "__slots__", ()))

    assert "secret_provider" in parameters
    assert "secret_ref" in parameters
    assert not {"api_key", "access_token", "token", "credential"} & set(parameters)
    assert not {"_api_key", "_access_token", "_token", "_credential"} & slots
