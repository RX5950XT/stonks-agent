"""Exchange-local trading sessions with UTC boundary outputs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    open_time: time
    close_time: time
    weekdays: frozenset[int] = frozenset(range(5))
    break_start: time | None = None
    break_end: time | None = None

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: frozenset[int]) -> frozenset[int]:
        if not value or any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays must contain values from 0 through 6")
        return value

    @model_validator(mode="after")
    def validate_break(self) -> Self:
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError("break_start and break_end must be provided together")
        if self.break_start is None or self.break_end is None:
            return self
        if self.close_time <= self.open_time:
            raise ValueError("breaks in overnight sessions are not supported")
        if not self.open_time < self.break_start < self.break_end < self.close_time:
            raise ValueError("session break must be inside the trading session")
        return self


class SessionOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_date: date
    template: SessionTemplate | None


class MarketSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    session_date: date
    opens_at: datetime
    closes_at: datetime
    break_start: datetime | None = None
    break_end: datetime | None = None

    @model_validator(mode="after")
    def validate_boundaries(self) -> Self:
        if self.opens_at.tzinfo is None or self.closes_at.tzinfo is None:
            raise ValueError("session boundaries must be timezone-aware")
        if self.closes_at <= self.opens_at:
            raise ValueError("session close must be after open")
        return self

    def is_open_at(self, value: datetime) -> bool:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        moment = value.astimezone(UTC)
        if not self.opens_at <= moment < self.closes_at:
            return False
        if self.break_start is None or self.break_end is None:
            return True
        return not self.break_start <= moment < self.break_end


class ExchangeCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    timezone: str = Field(min_length=1, max_length=64)
    default: SessionTemplate
    holidays: frozenset[date] = frozenset()
    overrides: tuple[SessionOverride, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def reject_duplicate_overrides(self) -> Self:
        dates = tuple(item.session_date for item in self.overrides)
        if len(dates) != len(set(dates)):
            raise ValueError("calendar overrides must have unique dates")
        return self

    def session_for(self, session_date: date) -> MarketSession | None:
        template = self._template_for(session_date)
        if template is None:
            return None
        zone = ZoneInfo(self.timezone)
        local_open = datetime.combine(session_date, template.open_time, zone)
        close_date = (
            session_date + timedelta(days=1)
            if template.close_time <= template.open_time
            else session_date
        )
        local_close = datetime.combine(close_date, template.close_time, zone)
        local_break_start = _combine_optional(
            session_date,
            template.break_start,
            zone,
        )
        local_break_end = _combine_optional(
            session_date,
            template.break_end,
            zone,
        )
        return MarketSession(
            mic=self.mic,
            session_date=session_date,
            opens_at=local_open.astimezone(UTC),
            closes_at=local_close.astimezone(UTC),
            break_start=(
                local_break_start.astimezone(UTC)
                if local_break_start is not None
                else None
            ),
            break_end=(
                local_break_end.astimezone(UTC) if local_break_end is not None else None
            ),
        )

    def next_session_after(self, value: datetime) -> MarketSession:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        moment = value.astimezone(UTC)
        local_date = moment.astimezone(ZoneInfo(self.timezone)).date()
        for offset in range(371):
            candidate = self.session_for(local_date + timedelta(days=offset))
            if candidate is not None and candidate.opens_at > moment:
                return candidate
        raise LookupError("no trading session found within 370 days")

    def _template_for(self, session_date: date) -> SessionTemplate | None:
        for item in self.overrides:
            if item.session_date == session_date:
                return item.template
        if (
            session_date in self.holidays
            or session_date.weekday() not in self.default.weekdays
        ):
            return None
        return self.default


def _combine_optional(
    session_date: date,
    value: time | None,
    zone: ZoneInfo,
) -> datetime | None:
    return None if value is None else datetime.combine(session_date, value, zone)
