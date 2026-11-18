from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_platform.backtesting.replay import Costs
from trading_platform.core.models import ManualClock, Quote, Signal
from trading_platform.market_data.candles import INDIA, Candle
from trading_platform.market_data.feed import SimulatedFeed
from trading_platform.risk.checks import RiskDenied, RiskGate, RiskLimits
from trading_platform.strategies.ema import EMACrossover


@dataclass(frozen=True)
class Settings:
    symbol: str
    start: date
    end: date
    fast: int = 2
    slow: int = 4
    units: int = 10
    initial_cash: Decimal = Decimal("100000")
    costs: Costs = Costs()
    limits: RiskLimits = RiskLimits(quote_age=timedelta(days=7))

    def __post_init__(self) -> None:
        if not self.symbol.startswith("NSE:") or not self.symbol[4:].strip():
            raise ValueError("Use an NSE symbol such as NSE:RELIANCE")
        if self.start > self.end:
            raise ValueError("Start date must be on or before end date")
        if not self.initial_cash.is_finite() or self.initial_cash <= 0:
            raise ValueError("Starting cash must be positive and finite")
        if not 0 < self.units <= self.limits.max_units:
            raise ValueError("Position size must be positive and within the risk limit")
        rates = (self.costs.fee_bps, self.costs.slippage_bps)
        if (
            any(not rate.is_finite() or rate < 0 for rate in rates)
            or sum(rates) >= 10_000
        ):
            raise ValueError(
                "Costs must be finite, nonnegative and total less than 10000 bps"
            )


@dataclass(frozen=True)
class Fill:
    signal_bar: datetime
    fill_bar: datetime
    units: int
    open_price: Decimal
    fee: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class Rejection:
    signal_bar: datetime
    execution_bar: datetime
    reason: str


@dataclass(frozen=True)
class Equity:
    bar: datetime
    cash: Decimal
    units: int
    value: Decimal


@dataclass(frozen=True)
class Report:
    settings: Settings
    signals: list[Signal]
    fills: list[Fill]
    rejections: list[Rejection]
    equity_curve: list[Equity]
    unfilled_signal: Signal | None
    final_equity: Decimal
    net_pnl: Decimal
    total_cost: Decimal
    max_drawdown_percent: Decimal
    trade_count: int


def backtest(candles: Iterable[Candle], settings: Settings) -> Report:
    strategy = EMACrossover(settings.fast, settings.slow, settings.units)
    feed = SimulatedFeed()
    clock = ManualClock(datetime.combine(settings.start, time.min, INDIA))
    risk = RiskGate(clock, feed, settings.limits, frozenset({strategy.name}))
    cash = peak = settings.initial_cash
    units = 0
    drawdown = Decimal(0)
    pending = None
    previous = None
    signals: list[Signal] = []
    fills: list[Fill] = []
    rejections: list[Rejection] = []
    equity: list[Equity] = []

    for candle in candles:
        if previous is not None and candle.timestamp <= previous:
            raise ValueError("Candle timestamps must be unique and strictly increasing")
        previous = candle.timestamp
        session = candle.timestamp.astimezone(INDIA).date()
        if session > settings.end:
            continue
        close = Quote(settings.symbol, candle.timestamp, candle.close)
        if session < settings.start:
            strategy.on_quote(close)
            continue

        if pending is not None:
            clock.current = candle.timestamp
            feed.publish(Quote(settings.symbol, candle.timestamp, candle.open))
            try:
                quantity = risk.check(pending, {settings.symbol: units}, killed=False)
                notional = quantity * candle.open
                fee = abs(notional) * settings.costs.fee_bps / 10_000
                slippage = abs(notional) * settings.costs.slippage_bps / 10_000
                next_cash = cash - notional - fee - slippage
                if next_cash < 0:
                    raise RiskDenied("Insufficient cash including costs")
                if quantity:
                    fills.append(
                        Fill(
                            pending.quote.timestamp,
                            candle.timestamp,
                            quantity,
                            candle.open,
                            fee,
                            slippage,
                        )
                    )
                    cash = next_cash
                    units += quantity
            except RiskDenied as exc:
                rejections.append(
                    Rejection(pending.quote.timestamp, candle.timestamp, str(exc))
                )

        value = cash + units * candle.close
        equity.append(Equity(candle.timestamp, cash, units, value))
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak * 100)
        pending = strategy.on_quote(close)
        if pending is not None:
            signals.append(pending)

    if not equity:
        raise ValueError("No candles in the selected India date range")
    final = equity[-1].value
    return Report(
        settings,
        signals,
        fills,
        rejections,
        equity,
        pending,
        final,
        final - settings.initial_cash,
        sum((fill.fee + fill.slippage for fill in fills), Decimal(0)),
        drawdown,
        len(fills),
    )
