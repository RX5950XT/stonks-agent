// Stonks Agent clean-room adapter code. Copyright 2026 Stonks Agent contributors.
// Licensed under Apache-2.0. This file is not copied from QuantConnect LEAN.

using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using QuantConnect;
using QuantConnect.Algorithm;
using QuantConnect.Orders;
using QuantConnect.Orders.Fees;
using QuantConnect.Orders.Slippage;

namespace Stonks.Lean;

public sealed class CanonicalBacktestAlgorithm : QCAlgorithm
{
    private const string EngineVersion =
        "17917+c22774e49ee80ecef5ca84f57616f6b66fad8bc5";
    private const string TagPrefix = "stonks-child:";
    private readonly List<TraceEvent> _events = [];
    private readonly Dictionary<string, Symbol> _symbols =
        new(StringComparer.Ordinal);
    private string _tracePath = "";

    public override void Initialize()
    {
        var schedulePath = GetParameter("schedule_path");
        _tracePath = GetParameter("trace_path");
        ValidatePaths(schedulePath, _tracePath);
        var schedule = ReadSchedule(schedulePath);
        if (schedule.EngineVersion != EngineVersion)
        {
            throw new InvalidOperationException("LEAN schedule identity changed");
        }

        SetTimeZone(TimeZones.Utc);
        SetStartDate(schedule.StartDate.Year, schedule.StartDate.Month, schedule.StartDate.Day);
        SetEndDate(schedule.EndDate.Year, schedule.EndDate.Month, schedule.EndDate.Day);
        SetCash(ParsePositive(schedule.Cash, "cash"));

        foreach (var item in schedule.Instruments)
        {
            var security = AddEquity(
                item.Symbol,
                Resolution.Minute,
                Market.USA,
                fillForward: false,
                leverage: 1m,
                extendedMarketHours: true,
                dataNormalizationMode: DataNormalizationMode.Raw);
            security.SetFeeModel(new ConstantFeeModel(0m));
            security.SetSlippageModel(new ConstantSlippageModel(0m));
            _symbols.Add(item.Symbol, security.Symbol);
        }

        foreach (var child in schedule.Children)
        {
            var submission = child.SubmitAtUtc.UtcDateTime;
            Schedule.On(
                DateRules.On(submission.Year, submission.Month, submission.Day),
                TimeRules.At(
                    submission.Hour,
                    submission.Minute,
                    submission.Second,
                    TimeZones.Utc),
                () => Submit(child));
        }
    }

    public override void OnOrderEvent(OrderEvent orderEvent)
    {
        var order = Transactions.GetOrderById(orderEvent.OrderId);
        if (order?.Tag is null || !order.Tag.StartsWith(TagPrefix, StringComparison.Ordinal))
        {
            return;
        }

        _events.Add(new TraceEvent
        {
            ChildId = order.Tag[TagPrefix.Length..],
            LeanOrderId = orderEvent.OrderId,
            OrderEventId = orderEvent.Id,
            Status = orderEvent.Status.ToString(),
            Direction = orderEvent.Direction.ToString(),
            FillQuantity = Format(orderEvent.FillQuantity),
            FillPrice = Format(orderEvent.FillPrice),
            OrderFee = Format(orderEvent.OrderFee.Value.Amount),
            UtcTime = orderEvent.UtcTime.ToString("O", CultureInfo.InvariantCulture),
        });
    }

    public override void OnEndOfAlgorithm()
    {
        var trace = new TraceDocument
        {
            EngineVersion = EngineVersion,
            Events = _events,
        };
        File.WriteAllText(
            _tracePath,
            JsonSerializer.Serialize(trace, JsonOptions),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    private void Submit(ScheduleChild child)
    {
        var unsignedQuantity = ParsePositive(child.Quantity, "quantity");
        var quantity = child.Side switch
        {
            "buy" => unsignedQuantity,
            "sell" => -unsignedQuantity,
            _ => throw new InvalidOperationException("Unsupported side"),
        };
        var properties = new OrderProperties
        {
            TimeInForce = child.TimeInForce switch
            {
                "day" => TimeInForce.Day,
                "gtc" => TimeInForce.GoodTilCanceled,
                // LEAN has no native IOC. Canonical scheduler creates only the first child.
                "ioc" => TimeInForce.Day,
                _ => throw new InvalidOperationException("Unsupported time in force"),
            },
        };
        var tag = TagPrefix + child.ChildId;
        if (!_symbols.TryGetValue(child.Symbol, out var symbol))
        {
            throw new InvalidOperationException("Unknown scheduled symbol");
        }
        if (child.OrderType == "market")
        {
            MarketOrder(symbol, quantity, asynchronous: true, tag, properties);
            return;
        }
        if (child.OrderType != "limit" || child.NativeLimitPrice is null)
        {
            throw new InvalidOperationException("Unsupported order type");
        }
        LimitOrder(
            symbol,
            quantity,
            ParsePositive(child.NativeLimitPrice, "native_limit_price"),
            asynchronous: true,
            tag,
            properties);
    }

    private static ScheduleDocument ReadSchedule(string path)
    {
        if (new FileInfo(path).Length > 16 * 1024 * 1024)
        {
            throw new InvalidOperationException("LEAN schedule exceeds size limit");
        }
        return JsonSerializer.Deserialize<ScheduleDocument>(
            File.ReadAllText(path, Encoding.UTF8),
            JsonOptions) ?? throw new InvalidOperationException("LEAN schedule is invalid");
    }

    private static void ValidatePaths(string schedulePath, string tracePath)
    {
        var schedule = Path.GetFullPath(schedulePath);
        var trace = Path.GetFullPath(tracePath);
        if (!File.Exists(schedule)
            || Path.GetDirectoryName(schedule) != Path.GetDirectoryName(trace))
        {
            throw new InvalidOperationException("LEAN job paths are not scoped");
        }
    }

    private static string Format(decimal value) =>
        value.ToString(CultureInfo.InvariantCulture);

    private static decimal ParsePositive(string value, string field)
    {
        if (!decimal.TryParse(
                value,
                NumberStyles.Number,
                CultureInfo.InvariantCulture,
                out var parsed) || parsed <= 0m)
        {
            throw new InvalidOperationException($"Invalid {field}");
        }
        return parsed;
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = false,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        WriteIndented = false,
        MaxDepth = 16,
    };
}

public sealed class ScheduleDocument
{
    public required string EngineVersion { get; init; }
    public required DateOnly StartDate { get; init; }
    public required DateOnly EndDate { get; init; }
    public required string Cash { get; init; }
    public required List<InstrumentSpec> Instruments { get; init; }
    public required List<ScheduleChild> Children { get; init; }
}

public sealed class InstrumentSpec
{
    public required string Symbol { get; init; }
}

public sealed class ScheduleChild
{
    public required string ChildId { get; init; }
    public required string CanonicalOrderId { get; init; }
    public required string Symbol { get; init; }
    public required string Side { get; init; }
    public required string OrderType { get; init; }
    public required string TimeInForce { get; init; }
    public required string Quantity { get; init; }
    public string? NativeLimitPrice { get; init; }
    public required string SourceBarId { get; init; }
    public required DateTimeOffset SourceOpensAtUtc { get; init; }
    public required DateTimeOffset SubmitAtUtc { get; init; }
}

public sealed class TraceDocument
{
    public required string EngineVersion { get; init; }
    public required List<TraceEvent> Events { get; init; }
}

public sealed class TraceEvent
{
    public required string ChildId { get; init; }
    public required int LeanOrderId { get; init; }
    public required int OrderEventId { get; init; }
    public required string Status { get; init; }
    public required string Direction { get; init; }
    public required string FillQuantity { get; init; }
    public required string FillPrice { get; init; }
    public required string OrderFee { get; init; }
    public required string UtcTime { get; init; }
}
