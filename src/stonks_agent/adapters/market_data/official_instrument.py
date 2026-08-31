"""SEC and TWSE public instrument data with bounded, point-in-time reads."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import TypedDict

import httpx

from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.instrument_data import (
    InstrumentDataQuery,
    InstrumentFact,
    InstrumentFiling,
    InstrumentObservation,
    InstrumentOverview,
)
from stonks_agent.domain.market_region import market_for_symbol
from stonks_agent.ports.instrument_data import InstrumentDataSource

SEC_DATA_ORIGIN = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TWSE_ORIGIN = "https://openapi.twse.com.tw/v1"
SEC_SUBMISSIONS_PATH = "/submissions/CIK{cik}.json"
SEC_FACTS_PATH = "/api/xbrl/companyfacts/CIK{cik}.json"
TWSE_ENDPOINTS = {
    "monthly_revenue": "/opendata/t187ap05_L",
    "income_statement": "/opendata/t187ap06_L_ci",
    "balance_sheet": "/opendata/t187ap07_L_ci",
}
_MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 8.0
_CACHE_SECONDS = 300.0
_MAX_REQUESTS_PER_MINUTE = 60

_SEC_FACTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "revenue",
        "營收",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ),
    ),
    ("gross_profit", "毛利", ("GrossProfit",)),
    ("operating_income", "營業利益", ("OperatingIncomeLoss",)),
    (
        "operating_cash_flow",
        "營業現金流",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    (
        "capex",
        "資本支出",
        ("PaymentsToAcquirePropertyPlantAndEquipment",),
    ),
    ("operating_expenses", "營業費用", ("OperatingExpenses",)),
    (
        "dividends",
        "普通股股利",
        ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    ),
    ("net_income", "淨利", ("NetIncomeLoss",)),
    ("assets", "資產總額", ("Assets",)),
    ("liabilities", "負債總額", ("Liabilities",)),
    (
        "equity",
        "股東權益",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    ("eps", "每股盈餘", ("EarningsPerShareDiluted", "EarningsPerShareBasic")),
    ("cash", "現金及約當現金", ("CashAndCashEquivalentsAtCarryingValue",)),
    ("shares", "流通股數", ("EntityCommonStockSharesOutstanding",)),
)


@dataclass(frozen=True, slots=True)
class _CachedPayload:
    value: object
    observed_at: datetime
    expires_at: float


class _SecFactCandidate(TypedDict):
    value: str
    unit: str
    period: str
    event_time: datetime
    published_at: datetime
    filed_at: datetime
    label: str


class OfficialInstrumentDataSource(InstrumentDataSource):
    """One allowlisted source for each verified market."""

    __slots__ = (
        "_cache",
        "_cache_lock",
        "_client",
        "_clock",
        "_monotonic",
        "_request_lock",
        "_requests",
        "_timeout",
        "_user_agent",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        user_agent: str = "stonks-agent/0.2",
        clock: Callable[[], datetime] = utc_now,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not user_agent or user_agent.strip() != user_agent:
            raise ValueError("SEC user agent is invalid")
        self._client = client
        self._user_agent = user_agent
        self._clock = clock
        self._monotonic = monotonic_clock
        self._timeout = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
        self._cache: dict[str, _CachedPayload] = {}
        self._cache_lock = Lock()
        self._requests: deque[float] = deque()
        self._request_lock = Lock()

    def fetch(
        self,
        query: InstrumentDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[InstrumentOverview]:
        observed = _aware_utc(observed_at)
        if isinstance(observed, Failure):
            return observed
        if observed.value > query.as_of:
            return _failure(
                ErrorCode.CONFLICT, "Instrument data is newer than its as_of"
            )
        market = market_for_symbol(query.symbol)
        try:
            if market == "US":
                return self._sec(query, observed.value)
            if market == "TW":
                return self._twse(query, observed.value)
            return _failure(
                ErrorCode.CAPABILITY_DENIED,
                "Instrument data market is not supported",
            )
        except (KeyError, TypeError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Official instrument data is invalid")

    def _sec(
        self,
        query: InstrumentDataQuery,
        observed_at: datetime,
    ) -> Result[InstrumentOverview]:
        tickers = self._json(
            SEC_TICKERS_URL,
            headers={"Accept": "application/json", "User-Agent": self._user_agent},
            observed_at=observed_at,
            as_of=query.as_of,
            cache_seconds=3600.0,
        )
        if isinstance(tickers, Failure):
            return tickers
        cik = _find_cik(tickers.value, query.symbol)
        if cik is None:
            return _failure(ErrorCode.NOT_FOUND, "SEC ticker was not found")
        submissions = self._json(
            f"{SEC_DATA_ORIGIN}{SEC_SUBMISSIONS_PATH.format(cik=cik)}",
            headers={"Accept": "application/json", "User-Agent": self._user_agent},
            observed_at=observed_at,
            as_of=query.as_of,
            cache_seconds=_CACHE_SECONDS,
        )
        facts = self._json(
            f"{SEC_DATA_ORIGIN}{SEC_FACTS_PATH.format(cik=cik)}",
            headers={"Accept": "application/json", "User-Agent": self._user_agent},
            observed_at=observed_at,
            as_of=query.as_of,
            cache_seconds=_CACHE_SECONDS,
        )
        if isinstance(submissions, Failure) and isinstance(facts, Failure):
            return submissions
        warnings: list[str] = []
        submission_payload = (
            submissions.value if isinstance(submissions, Success) else {}
        )
        facts_payload = facts.value if isinstance(facts, Success) else {}
        if isinstance(submissions, Failure):
            warnings.append("sec_submissions_unavailable")
        if isinstance(facts, Failure):
            warnings.append("sec_companyfacts_unavailable")
        identity = _sec_identity(submission_payload, query.symbol)
        parsed_facts = _sec_facts(
            facts_payload,
            query=query,
            observed_at=observed_at,
            cik=cik,
        )
        filings = _sec_filings(
            submission_payload,
            query=query,
            observed_at=observed_at,
            cik=cik,
        )
        if not parsed_facts and not filings:
            return _failure(
                ErrorCode.NOT_FOUND, "SEC returned no usable instrument data"
            )
        return Success(
            InstrumentOverview(
                symbol=query.symbol,
                market="US",
                name=identity[0],
                exchange=identity[1],
                industry=identity[2],
                cik=cik,
                state="partial" if warnings else "available",
                provider="sec",
                observed_at=observed_at,
                as_of=query.as_of,
                facts=parsed_facts,
                filings=filings,
                warnings=tuple(warnings),
            )
        )

    def _twse(
        self,
        query: InstrumentDataQuery,
        observed_at: datetime,
    ) -> Result[InstrumentOverview]:
        code = query.symbol.rsplit(".", 1)[0]
        rows: dict[str, tuple[Mapping[str, object], ...]] = {}
        warnings: list[str] = []
        for category, path in TWSE_ENDPOINTS.items():
            result = self._json(
                f"{TWSE_ORIGIN}{path}",
                headers={"Accept": "application/json"},
                observed_at=observed_at,
                as_of=query.as_of,
                cache_seconds=_CACHE_SECONDS,
            )
            if isinstance(result, Failure):
                warnings.append(f"twse_{category}_unavailable")
                continue
            found = _find_twse_rows(
                result.value,
                code,
                query.as_of,
                observed_at,
            )
            if found:
                rows[category] = found
        if not rows:
            if warnings:
                return _failure(
                    ErrorCode.DATA_UNAVAILABLE, "TWSE instrument data is unavailable"
                )
            return _failure(ErrorCode.NOT_FOUND, "TWSE ticker was not found")
        facts = _twse_facts(rows, query=query, observed_at=observed_at)
        if not facts:
            return _failure(
                ErrorCode.NOT_FOUND, "TWSE returned no usable financial data"
            )
        first = next(iter(rows.values()))[0]
        return Success(
            InstrumentOverview(
                symbol=query.symbol,
                market="TW",
                name=_row_text(first, "公司名稱") or code,
                exchange="TWSE",
                industry=_row_text(first, "產業別"),
                state="partial"
                if warnings or len(rows) < len(TWSE_ENDPOINTS)
                else "available",
                provider="twse_openapi",
                observed_at=observed_at,
                as_of=query.as_of,
                facts=facts,
                warnings=tuple(warnings),
            )
        )

    def _json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        observed_at: datetime,
        as_of: datetime,
        cache_seconds: float,
    ) -> Result[object]:
        cached = self._cached(url, as_of)
        if cached is not None:
            return Success(cached)
        if not self._consume_request():
            return _failure(
                ErrorCode.RATE_LIMITED, "Official data request budget is exhausted"
            )
        deadline = response_deadline(self._monotonic, _REQUEST_TIMEOUT_SECONDS)
        if deadline is None:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED, "Official data deadline is invalid"
            )
        try:
            with self._client.stream(
                "GET",
                url,
                headers=dict(headers) | {"Accept-Encoding": "identity"},
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    return _http_failure(response.status_code)
                media_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip()
                )
                if media_type != "application/json":
                    return _failure(
                        ErrorCode.DATA_UNAVAILABLE,
                        "Official data response format is invalid",
                    )
                body = read_bounded_raw(
                    response,
                    max_bytes=_MAX_RESPONSE_BYTES,
                    deadline=deadline,
                    clock=self._monotonic,
                )
        except httpx.TimeoutException:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED, "Official data request timed out"
            )
        except httpx.HTTPError:
            return _failure(
                ErrorCode.DATA_UNAVAILABLE, "Official data provider is unavailable"
            )
        if isinstance(body, ResponseBodyError):
            return _failure(
                ErrorCode.PAYLOAD_TOO_LARGE
                if body is ResponseBodyError.RESPONSE_TOO_LARGE
                else ErrorCode.DEADLINE_EXCEEDED,
                "Official data response could not be read",
            )
        try:
            value = json.loads(body, parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            return _failure(
                ErrorCode.DATA_UNAVAILABLE, "Official data response is invalid"
            )
        if not isinstance(value, (Mapping, list)):
            return _failure(
                ErrorCode.DATA_UNAVAILABLE, "Official data response shape is invalid"
            )
        self._store(url, value, observed_at, cache_seconds)
        return Success(value)

    def _cached(self, url: str, as_of: datetime) -> object | None:
        now = self._monotonic()
        with self._cache_lock:
            value = self._cache.get(url)
            if value is None or value.expires_at <= now or value.observed_at > as_of:
                if value is not None and value.expires_at <= now:
                    self._cache.pop(url, None)
                return None
            return value.value

    def _store(
        self,
        url: str,
        value: object,
        observed_at: datetime,
        cache_seconds: float,
    ) -> None:
        with self._cache_lock:
            self._cache[url] = _CachedPayload(
                value=value,
                observed_at=observed_at,
                expires_at=self._monotonic() + cache_seconds,
            )

    def _consume_request(self) -> bool:
        now = self._monotonic()
        with self._request_lock:
            while self._requests and now - self._requests[0] >= 60.0:
                self._requests.popleft()
            if len(self._requests) >= _MAX_REQUESTS_PER_MINUTE:
                return False
            self._requests.append(now)
            return True


def _find_cik(payload: object, symbol: str) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    wanted = symbol.upper().replace(".", "-")
    for item in payload.values():
        if not isinstance(item, Mapping):
            continue
        ticker = item.get("ticker")
        raw_cik = item.get("cik_str")
        if not isinstance(ticker, str) or ticker.upper().replace(".", "-") != wanted:
            continue
        if isinstance(raw_cik, int) and not isinstance(raw_cik, bool):
            return f"{raw_cik:010d}"
        if isinstance(raw_cik, str) and raw_cik.isdecimal():
            return raw_cik.zfill(10)
    return None


def _sec_identity(payload: object, symbol: str) -> tuple[str, str | None, str | None]:
    if not isinstance(payload, Mapping):
        return symbol, None, None
    name = _safe_value(payload.get("name")) or symbol
    exchange = _safe_value(_first_value(payload.get("exchanges")))
    industry = _safe_value(payload.get("sicDescription"))
    return name, exchange, industry


def _sec_facts(
    payload: object,
    *,
    query: InstrumentDataQuery,
    observed_at: datetime,
    cik: str,
) -> tuple[InstrumentFact, ...]:
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("facts"), Mapping
    ):
        return ()
    root = payload["facts"]
    values: list[InstrumentFact] = []
    source_url = f"{SEC_DATA_ORIGIN}{SEC_FACTS_PATH.format(cik=cik)}"
    for key, label, concepts in _SEC_FACTS:
        history = _sec_fact_history(
            root,
            concepts,
            min(query.as_of.date(), observed_at.date()),
        )
        if not history:
            continue
        candidate = history[0]
        values.append(
            InstrumentFact(
                key=key,
                label=label,
                value=candidate["value"],
                unit=candidate["unit"],
                period=candidate["period"],
                event_time=candidate["event_time"],
                published_at=candidate["published_at"],
                available_at=candidate["published_at"],
                provider="sec",
                source_url=source_url,
                history=tuple(
                    InstrumentObservation(
                        value=item["value"],
                        unit=item["unit"],
                        period=item["period"],
                        event_time=item["event_time"],
                        published_at=item["published_at"],
                        available_at=item["published_at"],
                    )
                    for item in history
                ),
            )
        )
    return tuple(values)


def _sec_fact_history(
    root: object,
    concepts: Sequence[str],
    as_of: date,
) -> tuple[_SecFactCandidate, ...]:
    if not isinstance(root, Mapping):
        return ()
    candidates: dict[tuple[str, str], _SecFactCandidate] = {}
    for namespace in root.values():
        if not isinstance(namespace, Mapping):
            continue
        for concept in concepts:
            item = namespace.get(concept)
            if not isinstance(item, Mapping) or not isinstance(
                item.get("units"), Mapping
            ):
                continue
            label = _safe_value(item.get("label")) or concept
            units = item["units"]
            for unit_name, records in units.items():
                if not isinstance(unit_name, str) or not isinstance(records, list):
                    continue
                for record in records:
                    parsed = _sec_record(record, unit_name, label, as_of)
                    if parsed is not None:
                        key = (parsed["period"], parsed["unit"])
                        previous = candidates.get(key)
                        if previous is None or (
                            parsed["filed_at"],
                            parsed["event_time"],
                        ) > (previous["filed_at"], previous["event_time"]):
                            candidates[key] = parsed
    return tuple(
        sorted(
            candidates.values(),
            key=lambda value: (value["event_time"], value["filed_at"]),
            reverse=True,
        )[:12]
    )


def _sec_record(
    record: object,
    unit_name: str,
    label: str,
    as_of: date,
) -> _SecFactCandidate | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("val")
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite():
        return None
    filed = _date_value(record.get("filed"))
    end = _date_value(record.get("end")) or filed
    if filed is None or filed.date() > as_of or end is None or end.date() > as_of:
        return None
    start = _date_value(record.get("start"))
    period = (
        f"{start.date().isoformat()} — {end.date().isoformat()}"
        if start is not None
        else end.date().isoformat()
    )
    return {
        "value": _decimal_text(decimal),
        "unit": unit_name,
        "period": period,
        "event_time": end,
        "published_at": filed,
        "filed_at": filed,
        "label": label,
    }


def _sec_filings(
    payload: object,
    *,
    query: InstrumentDataQuery,
    observed_at: datetime,
    cik: str,
) -> tuple[InstrumentFiling, ...]:
    if not isinstance(payload, Mapping):
        return ()
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, Mapping) else None
    if not isinstance(recent, Mapping):
        return ()
    forms = recent.get("form")
    filed = recent.get("filingDate")
    periods = recent.get("reportDate")
    accessions = recent.get("accessionNumber")
    documents = recent.get("primaryDocument")
    descriptions = recent.get("primaryDocDescription")
    if (
        not isinstance(forms, list)
        or not isinstance(filed, list)
        or not isinstance(accessions, list)
        or not isinstance(documents, list)
    ):
        return ()
    result: list[InstrumentFiling] = []
    for index, raw_form in enumerate(forms):
        values = (filed, accessions, documents)
        if any(index >= len(value) for value in values):
            continue
        filed_at = _date_value(filed[index])
        accession = accessions[index]
        document = documents[index]
        if filed_at is None or filed_at.date() > min(
            query.as_of.date(), observed_at.date()
        ):
            continue
        if (
            not isinstance(raw_form, str)
            or not isinstance(accession, str)
            or not isinstance(document, str)
            or not _valid_accession(accession)
            or not _valid_document(document)
        ):
            continue
        period_end = (
            _date_value(periods[index])
            if isinstance(periods, list) and index < len(periods)
            else None
        )
        description = (
            descriptions[index]
            if isinstance(descriptions, list)
            and index < len(descriptions)
            and isinstance(descriptions[index], str)
            else None
        )
        accession_path = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{document}"
        result.append(
            InstrumentFiling(
                form=raw_form,
                filed_at=filed_at,
                period_end=period_end,
                description=description,
                provider="sec",
                source_url=url,
            )
        )
        if len(result) >= 20:
            break
    return tuple(result)


def _twse_facts(
    rows: Mapping[str, tuple[Mapping[str, object], ...]],
    *,
    query: InstrumentDataQuery,
    observed_at: datetime,
) -> tuple[InstrumentFact, ...]:
    fields = {
        "monthly_revenue": (
            (("營業收入-當月營收", "當月營收"), "monthly_revenue", "TWD", "當月營收"),
            (
                ("營業收入-上月營收", "上月營收"),
                "previous_month_revenue",
                "TWD",
                "上月營收",
            ),
            (
                ("營業收入-去年當月營收", "去年當月營收"),
                "same_month_last_year_revenue",
                "TWD",
                "去年當月營收",
            ),
            (
                ("營業收入-去年同月增減(%)", "營收成長率%"),
                "revenue_yoy_percent",
                "%",
                "營收年增率",
            ),
            (
                ("累計營業收入-當月累計營收", "累計營收"),
                "cumulative_revenue",
                "TWD",
                "累計營收",
            ),
            (
                ("累計營業收入-前期比較增減(%)", "累計營收成長率%"),
                "cumulative_revenue_yoy_percent",
                "%",
                "累計營收年增率",
            ),
        ),
        "income_statement": (
            (("營業收入",), "revenue", "TWD", "營業收入"),
            (
                ("營業毛利（毛損）", "營業毛利"),  # noqa: RUF001
                "gross_profit",
                "TWD",
                "營業毛利",
            ),
            (
                ("營業利益\uff08損失\uff09", "營業利益"),
                "operating_income",
                "TWD",
                "營業利益",
            ),
            (
                ("稅前淨利\uff08淨損\uff09", "稅前淨利"),
                "pre_tax_income",
                "TWD",
                "稅前淨利",
            ),
            (("本期淨利\uff08淨損\uff09", "本期淨利"), "net_income", "TWD", "本期淨利"),
            (
                ("本期綜合損益總額",),
                "comprehensive_income",
                "TWD",
                "綜合損益總額",
            ),
            (
                ("基本每股盈餘\uff08元\uff09", "每股盈餘"),
                "eps",
                "TWD/share",
                "每股盈餘",
            ),
        ),
        "balance_sheet": (
            (("流動資產",), "current_assets", "TWD", "流動資產"),
            (("資產總計", "資產總額"), "assets", "TWD", "資產總額"),
            (("流動負債",), "current_liabilities", "TWD", "流動負債"),
            (("負債總計", "負債總額"), "liabilities", "TWD", "負債總額"),
            (("權益總計", "權益總額"), "equity", "TWD", "權益總額"),
            (
                ("每股參考淨值",),
                "book_value_per_share",
                "TWD/share",
                "每股參考淨值",
            ),
        ),
    }
    facts: list[InstrumentFact] = []
    for category, category_rows in rows.items():
        source_url = f"{TWSE_ORIGIN}{TWSE_ENDPOINTS[category]}"
        for labels, key, unit, label in fields[category]:
            observations: list[InstrumentObservation] = []
            for row in category_rows:
                published_at = _twse_published_at(row, query.as_of, observed_at)
                value = _row_text_any(row, labels)
                if published_at is None or value is None:
                    continue
                observations.append(
                    InstrumentObservation(
                        value=value,
                        unit=unit,
                        period=_twse_period(row),
                        event_time=published_at,
                        published_at=published_at,
                        available_at=published_at,
                    )
                )
            if not observations:
                continue
            latest = observations[0]
            facts.append(
                InstrumentFact(
                    key=key,
                    label=label,
                    value=latest.value,
                    unit=unit,
                    period=latest.period,
                    event_time=latest.event_time,
                    published_at=latest.published_at,
                    available_at=latest.available_at,
                    provider="twse_openapi",
                    source_url=source_url,
                    history=tuple(observations),
                )
            )
    return tuple(facts)


def _find_twse_rows(
    payload: object,
    code: str,
    as_of: datetime,
    observed_at: datetime,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, list):
        return ()
    rows: list[Mapping[str, object]] = []
    for item in payload:
        if not isinstance(item, Mapping) or _row_text(item, "公司代號") != code:
            continue
        if _twse_published_at(item, as_of, observed_at) is not None:
            rows.append(item)
    ordered = sorted(
        rows,
        key=lambda row: (
            _twse_published_at(row, as_of, observed_at)
            or datetime.min.replace(tzinfo=UTC),
            _twse_period(row) or "",
        ),
        reverse=True,
    )
    result: list[Mapping[str, object]] = []
    periods: set[str] = set()
    for row in ordered:
        period = _twse_period(row) or (
            f"published:{_twse_published_at(row, as_of, observed_at)}"
        )
        if period in periods:
            continue
        periods.add(period)
        result.append(row)
        if len(result) >= 12:
            break
    return tuple(result)


def _twse_published_at(
    row: Mapping[str, object],
    as_of: datetime,
    observed_at: datetime,
) -> datetime | None:
    raw = _row_text(row, "出表日期")
    parsed = _date_value(raw) if raw else observed_at
    twse_cutoff = min(as_of, observed_at).astimezone(UTC) + timedelta(hours=8)
    if parsed is None or parsed.date() > twse_cutoff.date():
        return None
    return parsed


def _twse_period(row: Mapping[str, object]) -> str | None:
    month = _row_text(row, "資料年月")
    if month:
        if month.isdecimal() and len(month) == 5:
            return f"{int(month[:3]) + 1911}-{month[3:]}"
        if month.isdecimal() and len(month) == 6:
            return f"{month[:4]}-{month[4:]}"
        return month
    year = _row_text(row, "年度")
    quarter = _row_text(row, "季別")
    if year and quarter:
        if year.isdecimal() and len(year) == 3:
            year = str(int(year) + 1911)
        number = quarter.replace("第", "").replace("季", "").strip()
        return f"{year} Q{number}"
    return year or quarter


def _row_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    rendered = str(value).strip()
    if not rendered or rendered in {"-", "--", "N/A", "null"}:
        return None
    return rendered if all(ord(character) >= 32 for character in rendered) else None


def _row_text_any(row: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = _row_text(row, key)
        if value is not None:
            return value
    return None


def _first_value(value: object) -> object | None:
    return value[0] if isinstance(value, list) and value else None


def _safe_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip()
    return (
        rendered
        if rendered and all(ord(character) >= 32 for character in rendered)
        else None
    )


def _date_value(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip().replace("/", "-")
    if rendered.isdecimal() and len(rendered) == 7:
        try:
            parsed = date(
                int(rendered[:3]) + 1911,
                int(rendered[3:5]),
                int(rendered[5:]),
            )
        except ValueError:
            return None
    else:
        try:
            parsed = date.fromisoformat(rendered)
        except ValueError:
            return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _valid_accession(value: str) -> bool:
    return (
        len(value) == 20
        and value[10] == "-"
        and value[13] == "-"
        and value.replace("-", "").isdigit()
    )


def _valid_document(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(
        character.isalnum() or character in "._-" for character in value
    )


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _aware_utc(value: datetime) -> Result[datetime]:
    if value.tzinfo is None or value.utcoffset() is None:
        return _failure(
            ErrorCode.CONFIGURATION_INVALID, "Instrument data clock is invalid"
        )
    return Success(value.astimezone(UTC))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _http_failure(status: int) -> Failure:
    if status in {httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE}:
        return _failure(
            ErrorCode.RATE_LIMITED, "Official data provider rate limit reached"
        )
    if status == httpx.codes.NOT_FOUND:
        return _failure(ErrorCode.NOT_FOUND, "Official data was not found")
    return _failure(ErrorCode.DATA_UNAVAILABLE, "Official data provider is unavailable")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
