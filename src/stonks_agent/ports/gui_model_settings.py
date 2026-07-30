"""Session-scoped model settings authority exposed to the loopback GUI."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelSettingsView,
)


@runtime_checkable
class GuiModelSettingsPort(Protocol):
    def view(self) -> GuiModelSettingsView: ...

    def configure(
        self,
        command: ConfigureGuiModelSettings,
    ) -> Result[GuiModelSettingsView]: ...

    def clear(self) -> Result[GuiModelSettingsView]: ...
