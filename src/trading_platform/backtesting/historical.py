from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_platform.backtesting.replay import Costs
from trading_platform.core.models import ManualClock, Quote, Signal
from trading_platform.market_data.candles import INDIA, Candle
from trading_platform.market_data.feed import SimulatedFeed
from trading_platform.risk.checks import RiskDenied, RiskGate, RiskLimits
from trading_platform.strategies.catalog import CATALOG, make_strategy


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

    stop_loss_percent: Decimal | None = None
    max_risk_percent: Decimal | None = None
    max_daily_loss_percent: Decimal | None = None
    strategy_id: str = CATALOG[0].id
    strategy_version: str = CATALOG[0].version

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

        for percent in (
            self.stop_loss_percent,
            self.max_risk_percent,
            self.max_daily_loss_percent,
        ):
            if percent is not None and (
                not percent.is_finite() or not 0 < percent < 100
            ):
                raise ValueError("Risk percentages must be above 0 and below 100")
        if self.max_risk_percent is not None and self.stop_loss_percent is None:
            raise ValueError("A risk budget requires a stop loss")


@dataclass(frozen=True)
class Fill:
    signal_bar: datetime
    fill_bar: datetime
    units: int
    open_price: Decimal
    fee: Decimal
    slippage: Decimal
    reason: str = "ema"
    execution_price: Decimal | None = None
    stop_price: Decimal | None = None

    @property
    def cash_change(self) -> Decimal:
        price = (
            self.open_price if self.execution_price is None else self.execution_price
        )
        return -self.units * price - self.fee - self.slippage


def priced_fill(
    candle: Candle,
    signal_bar: datetime,
    quantity: int,
    costs: Costs,
    execution_price: Decimal,
    reason: str,
    stop_price: Decimal | None,
) -> Fill:
    notional = abs(quantity) * execution_price
    return Fill(
        signal_bar,
        candle.timestamp,
        quantity,
        candle.open,
        notional * costs.fee_bps / 10_000,
        notional * costs.slippage_bps / 10_000,
        reason,
        execution_price,
        stop_price,
    )


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


def backtest(
    candles: Iterable[Candle],
    settings: Settings,
    blocked_entries: frozenset[datetime] = frozenset(),
) -> Report:
    strategy = make_strategy(
        settings.strategy_id,
        settings.strategy_version,
        settings.fast,
        settings.slow,
        settings.units,
    )
    feed = SimulatedFeed()
    clock = ManualClock(datetime.combine(settings.start, time.min, INDIA))
    risk = RiskGate(clock, feed, settings.limits, frozenset({settings.strategy_id}))
    cash = peak = settings.initial_cash
    units = 0
    session_day = None
    session_equity = cash
    session_halted = False
    drawdown = Decimal(0)
    pending = None
    previous = None
    stop: tuple[Decimal, datetime] | None = None
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

        if session != session_day:
            session_day = session
            session_equity = equity[-1].value if equity else cash
            session_halted = False

        # A down-gap stop already active at the open takes precedence over an EMA order.
        if stop is not None and candle.open <= stop[0]:
            fill = priced_fill(
                candle,
                stop[1],
                -units,
                settings.costs,
                candle.open,
                "stop_loss",
                stop[0],
            )
            fills.append(fill)
            cash += fill.cash_change
            units, stop, pending = 0, None, None

        daily_floor = None
        if settings.max_daily_loss_percent is not None:
            daily_floor = session_equity * (1 - settings.max_daily_loss_percent / 100)
            session_halted = session_halted or cash + units * candle.open <= daily_floor

        if pending is not None:
            clock.current = candle.timestamp
            feed.publish(Quote(settings.symbol, candle.timestamp, candle.open))
            try:
                quantity = risk.check(pending, {settings.symbol: units}, killed=False)
                if quantity > 0 and (
                    session_halted or candle.timestamp in blocked_entries
                ):
                    raise RiskDenied(
                        "Daily loss limit reached"
                        if session_halted
                        else "Deployment entries paused"
                    )
                if quantity:
                    trigger = None
                    if quantity > 0 and settings.stop_loss_percent is not None:
                        trigger = candle.open * (1 - settings.stop_loss_percent / 100)
                    fill = priced_fill(
                        candle,
                        pending.quote.timestamp,
                        quantity,
                        settings.costs,
                        candle.open,
                        "ema",
                        trigger,
                    )
                    if (
                        quantity > 0
                        and trigger is not None
                        and settings.max_risk_percent is not None
                    ):
                        exit_at_stop = priced_fill(
                            candle,
                            pending.quote.timestamp,
                            -quantity,
                            settings.costs,
                            trigger,
                            "stop_loss",
                            trigger,
                        )
                        planned_loss = -(fill.cash_change + exit_at_stop.cash_change)
                        budget = (
                            (cash + units * candle.open)
                            * settings.max_risk_percent
                            / 100
                        )
                        if planned_loss > budget:
                            raise RiskDenied(
                                "Estimated loss at the stop exceeds the risk budget"
                            )
                    if cash + fill.cash_change < 0:
                        raise RiskDenied("Insufficient cash including costs")
                    fills.append(fill)
                    cash += fill.cash_change
                    units += quantity
                    stop = (
                        (trigger, pending.quote.timestamp)
                        if trigger is not None
                        else None
                    )
            except RiskDenied as exc:
                rejections.append(
                    Rejection(pending.quote.timestamp, candle.timestamp, str(exc))
                )

        # The stop becomes active immediately after an opening fill and before the close.
        if stop is not None and candle.low <= stop[0]:
            fill = priced_fill(
                candle, stop[1], -units, settings.costs, stop[0], "stop_loss", stop[0]
            )
            fills.append(fill)
            cash += fill.cash_change
            units, stop = 0, None

        value = cash + units * candle.close
        if daily_floor is not None:
            session_halted = session_halted or value <= daily_floor
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
