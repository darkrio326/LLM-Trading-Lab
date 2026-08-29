"""Broker-independent synthetic execution and append-only experiment ledger.

This module is deliberately outside :mod:`zhixing.execution`.  ``BrokerAdapter`` remains the
boundary for real external broker side effects; the paper venue below only mutates the isolated
``ExperimentLedger`` under ``archive/_experiment`` and never implements or calls that protocol.

The ledger stores complete immutable state facts.  ``instruction_code`` identifies one synthetic
order, while a global file lock serializes cash and position changes across different orders.  A
restart reconstructs the account from the latest complete fact; no mutable balance snapshot is a
second source of truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import fcntl

from . import archive, experiment, tradingdays
from .catalog import (
    Catalog,
    TradeObject,
    TURNOVER_T0,
    TURNOVER_T1,
    VALID_ASSET_TYPES,
    VALID_MARKETS,
    VALID_TURNOVER_MODES,
)
from .guards import ObjectSnapshot, ValidatedOrder


MONEY = Decimal("0.01")
PCT = Decimal("0.0001")
EXPERIMENT_ID = "isolated-cny-1000-v1"
INITIAL_INSTRUCTION_CODE = "experiment:isolated-cny-1000-v1:initialize"


class ExperimentLedgerError(RuntimeError):
    """The durable synthetic account cannot be safely read or updated."""


def _decimal(value: object, *, default: Decimal = Decimal("0")) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _money(value: object) -> Decimal:
    return _decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _text(value: Decimal | object) -> str:
    return format(_decimal(value), "f")


def _iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _expires_at(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(15, 0), tzinfo=now.tzinfo)


class SyntheticOrderState(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


TERMINAL_ORDER_STATES = frozenset({
    SyntheticOrderState.FILLED,
    SyntheticOrderState.CANCELLED,
    SyntheticOrderState.EXPIRED,
    SyntheticOrderState.REJECTED,
})


@dataclass(frozen=True)
class SimulationFeeConfig:
    """Configurable experiment assumptions; these are not broker fee claims."""

    stock_commission_rate: Decimal = Decimal("0.0003")
    etf_commission_rate: Decimal = Decimal("0.0003")
    minimum_commission_cny: Decimal = Decimal("5")
    stock_sell_tax_rate: Decimal = Decimal("0.0005")
    etf_sell_tax_rate: Decimal = Decimal("0")
    stock_sell_fee_rate: Decimal = Decimal("0.00001")
    etf_sell_fee_rate: Decimal = Decimal("0")

    def as_entry(self) -> dict[str, str]:
        return {
            "stock_commission_rate": _text(self.stock_commission_rate),
            "etf_commission_rate": _text(self.etf_commission_rate),
            "minimum_commission_cny": _text(self.minimum_commission_cny),
            "stock_sell_tax_rate": _text(self.stock_sell_tax_rate),
            "etf_sell_tax_rate": _text(self.etf_sell_tax_rate),
            "stock_sell_fee_rate": _text(self.stock_sell_fee_rate),
            "etf_sell_fee_rate": _text(self.etf_sell_fee_rate),
        }


@dataclass(frozen=True)
class FeeQuote:
    commission: Decimal
    sell_tax: Decimal
    sell_fee: Decimal

    @property
    def total(self) -> Decimal:
        return _money(self.commission + self.sell_tax + self.sell_fee)

    def as_entry(self) -> dict[str, str]:
        return {
            "commission": _text(self.commission),
            "sell_tax": _text(self.sell_tax),
            "sell_fee": _text(self.sell_fee),
            "total": _text(self.total),
        }


@dataclass(frozen=True)
class SimulationFeeModel:
    config: SimulationFeeConfig = field(default_factory=SimulationFeeConfig)

    def quote(self, *, action: str, asset_type: str, notional: Decimal) -> FeeQuote:
        commission_rate = (
            self.config.etf_commission_rate
            if asset_type == "ETF"
            else self.config.stock_commission_rate
        )
        commission = max(
            self.config.minimum_commission_cny,
            notional * commission_rate,
        )
        sell_tax = Decimal("0")
        sell_fee = Decimal("0")
        if action == "sell":
            if asset_type == "ETF":
                sell_tax = notional * self.config.etf_sell_tax_rate
                sell_fee = notional * self.config.etf_sell_fee_rate
            else:
                sell_tax = notional * self.config.stock_sell_tax_rate
                sell_fee = notional * self.config.stock_sell_fee_rate
        return FeeQuote(
            commission=_money(commission),
            sell_tax=_money(sell_tax),
            sell_fee=_money(sell_fee),
        )


@dataclass(frozen=True)
class SimulationBenchmarkConfig:
    buy_and_hold_symbol: str | None = None


@dataclass(frozen=True)
class QuoteReference:
    object_id: str
    symbol: str
    price: Decimal
    observed_at: datetime
    usable: bool

    def as_entry(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "symbol": self.symbol,
            "price": _text(self.price),
            "observed_at": self.observed_at.isoformat(),
            "usable": self.usable,
        }

    @classmethod
    def from_entry(cls, raw: Mapping[str, Any]) -> "QuoteReference":
        observed = _iso(raw.get("observed_at"))
        if observed is None:
            raise ExperimentLedgerError("market reference 缺少合法 observed_at")
        return cls(
            object_id=str(raw.get("object_id") or ""),
            symbol=str(raw.get("symbol") or ""),
            price=_decimal(raw.get("price")),
            observed_at=observed,
            usable=bool(raw.get("usable")),
        )


@dataclass(frozen=True)
class SyntheticExecutionResult:
    instruction_code: str
    order_reference: str | None
    state: SyntheticOrderState
    fill_qty: int
    execution_price: Decimal | None
    fee: Decimal
    reasons: tuple[Mapping[str, str], ...] = ()

    @classmethod
    def from_order(cls, order: Mapping[str, Any]) -> "SyntheticExecutionResult":
        price = order.get("simulated_execution_price")
        raw_reasons = order.get("reasons") or ()
        return cls(
            instruction_code=str(order.get("instruction_code") or ""),
            order_reference=str(order.get("order_reference") or "") or None,
            state=SyntheticOrderState(str(order.get("state") or "REJECTED")),
            fill_qty=int(order.get("fill_qty") or 0),
            execution_price=_decimal(price) if price is not None else None,
            fee=_money(order.get("fee")),
            reasons=tuple(
                dict(item) for item in raw_reasons if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class SimulationVenueRules:
    """Market-mechanics checks, deliberately separate from ExperimentPolicy."""

    supported_markets: frozenset[str] = VALID_MARKETS
    supported_asset_types: frozenset[str] = VALID_ASSET_TYPES
    supported_turnover_modes: frozenset[str] = VALID_TURNOVER_MODES

    def buy_is_immediately_sellable(self, instrument: TradeObject) -> bool:
        """Only catalog-declared T+0 instruments unlock a BUY on its trade date."""
        return instrument.asset_type == "ETF" and instrument.turnover_mode == TURNOVER_T0

    def validate(
        self,
        order: ValidatedOrder,
        instrument: TradeObject | None,
        *,
        available_cash: Decimal,
        available_position_qty: int,
        fee_model: SimulationFeeModel,
    ) -> tuple[dict[str, str], ...]:
        reasons: list[dict[str, str]] = []
        if instrument is None or not instrument.is_tradable:
            reasons.append({
                "code": "UNSUPPORTED_INSTRUMENT",
                "message": "synthetic venue 只接受 catalog 中明确可交易的标的。",
            })
            return tuple(reasons)
        if instrument.market not in self.supported_markets:
            reasons.append({
                "code": "UNSUPPORTED_MARKET",
                "message": f"M1 synthetic venue 不支持市场 {instrument.market}。",
            })
        if instrument.asset_type not in self.supported_asset_types:
            reasons.append({
                "code": "UNSUPPORTED_ASSET_TYPE",
                "message": f"M1 synthetic venue 不支持资产类型 {instrument.asset_type}。",
            })
        if instrument.turnover_mode not in self.supported_turnover_modes:
            reasons.append({
                "code": "UNSUPPORTED_TURNOVER_MODE",
                "message": f"M1 synthetic venue 不支持回转制度 {instrument.turnover_mode}。",
            })
        elif instrument.turnover_mode == TURNOVER_T0 and instrument.asset_type != "ETF":
            reasons.append({
                "code": "T0_REQUIRES_ETF",
                "message": "M1 synthetic venue 只允许 catalog 中的 ETF 显式声明 T+0。",
            })
        if instrument.lot_size <= 0:
            reasons.append({
                "code": "UNKNOWN_LOT_SIZE",
                "message": "catalog 没有合法交易单位，不能猜测该品种的 lot-size。",
            })
        elif order.action == "buy" and order.qty % instrument.lot_size != 0:
            reasons.append({
                "code": "BUY_LOT_SIZE_VIOLATION",
                "message": f"BUY 数量 {order.qty} 不是交易单位 {instrument.lot_size} 的整数倍。",
            })
        if order.action == "sell" and order.qty > available_position_qty:
            reasons.append({
                "code": "OVERSELL_FORBIDDEN",
                "message": f"拟卖出 {order.qty}，synthetic 可卖数量为 {available_position_qty}。",
            })
        if order.action == "buy":
            reserve_notional = order.limit_price * order.qty
            reserve_fee = fee_model.quote(
                action="buy", asset_type=instrument.asset_type, notional=reserve_notional
            ).total
            required = _money(reserve_notional + reserve_fee)
            if required > available_cash:
                reasons.append({
                    "code": "INSUFFICIENT_SYNTHETIC_CASH",
                    "message": f"包含模拟费用后需要 {required} CNY，可用 synthetic cash 为 {available_cash} CNY。",
                })
        return tuple(reasons)


_REQUIRED_EVENT_FIELDS = frozenset({
    "experiment_id", "event_type", "strategy_id", "instruction_code", "object_id",
    "action", "qty", "model_limit_price", "simulated_execution_price", "order_state",
    "fill_qty", "fee", "cash_delta", "position_delta", "realized_pnl_delta",
    "resulting_cash", "resulting_position", "resulting_nav", "timestamp",
    "account_state", "orders_state",
})


def _empty_state(
    initial_cash: Decimal,
    benchmark: SimulationBenchmarkConfig,
    *,
    started_at: datetime | None,
) -> dict[str, Any]:
    cash = _money(initial_cash)
    return {
        "initial_cash": _text(cash),
        "cash": _text(cash),
        "positions": {},
        "realized_pnl": "0.00",
        "cumulative_fees": "0.00",
        "turnover": "0.00",
        "last_prices": {},
        "market_value": "0.00",
        "unrealized_pnl": "0.00",
        "nav": _text(cash),
        "high_water_mark": _text(cash),
        "current_drawdown_pct": "0",
        "max_drawdown_pct": "0",
        "started_at": started_at.isoformat() if started_at else None,
        "last_mark_at": None,
        "benchmark": {
            "cash_nav": _text(cash),
            "buy_and_hold_symbol": benchmark.buy_and_hold_symbol,
            "initial_price": None,
            "units": None,
            "last_price": None,
            "nav": None,
        },
    }


def _position_entry(
    *,
    instrument: TradeObject,
    qty: int,
    sellable_qty: int,
    pending_settlements: Sequence[Mapping[str, Any]],
    avg_cost: Decimal,
    last_price: Decimal,
) -> dict[str, Any]:
    pending = [copy.deepcopy(dict(item)) for item in pending_settlements]
    pending_qty = sum(int(item.get("qty") or 0) for item in pending)
    if qty < 0 or sellable_qty < 0 or sellable_qty + pending_qty != qty:
        raise ExperimentLedgerError(
            "position settlement invariant broken: "
            f"qty={qty} sellable={sellable_qty} pending={pending_qty}"
        )
    cost_basis = avg_cost * qty
    market_value = last_price * qty
    return {
        "object_id": instrument.object_id,
        "market": instrument.market,
        "symbol": instrument.symbol,
        "name": instrument.name,
        "asset_type": instrument.asset_type,
        "turnover_mode": instrument.turnover_mode,
        "qty": qty,
        "sellable_qty": sellable_qty,
        "pending_settlement_qty": pending_qty,
        "pending_settlements": pending,
        "average_cost": _text(avg_cost),
        "cost_basis": _text(_money(cost_basis)),
        "last_price": _text(last_price),
        "market_value": _text(_money(market_value)),
        "unrealized_pnl": _text(_money(market_value - cost_basis)),
    }


def _revalue(state: dict[str, Any], *, mark_at: datetime | None = None) -> None:
    market_value = Decimal("0")
    unrealized = Decimal("0")
    positions = state.get("positions") or {}
    last_prices = state.get("last_prices") or {}
    for symbol, raw in positions.items():
        qty = int(raw.get("qty") or 0)
        avg = _decimal(raw.get("average_cost"))
        last = _decimal(last_prices.get(symbol), default=avg)
        raw["last_price"] = _text(last)
        raw["cost_basis"] = _text(_money(avg * qty))
        raw["market_value"] = _text(_money(last * qty))
        raw["unrealized_pnl"] = _text(_money(last * qty - avg * qty))
        market_value += _money(last * qty)
        unrealized += _money(last * qty - avg * qty)

    cash = _money(state.get("cash"))
    nav = _money(cash + market_value)
    prior_high = _money(state.get("high_water_mark"))
    high = max(prior_high, nav)
    drawdown = (
        ((high - nav) / high * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)
        if high > 0 else Decimal("0")
    )
    state["market_value"] = _text(_money(market_value))
    state["unrealized_pnl"] = _text(_money(unrealized))
    state["nav"] = _text(nav)
    state["high_water_mark"] = _text(high)
    state["current_drawdown_pct"] = _text(drawdown)
    state["max_drawdown_pct"] = _text(max(
        _decimal(state.get("max_drawdown_pct")), drawdown
    ))
    if mark_at is not None:
        state["last_mark_at"] = mark_at.isoformat()


def _available_cash(
    state: Mapping[str, Any], orders: Mapping[str, Mapping[str, Any]], fee_model: SimulationFeeModel,
    *, excluding_instruction: str | None = None,
) -> Decimal:
    reserved = Decimal("0")
    for code, order in orders.items():
        if code == excluding_instruction or order.get("state") != SyntheticOrderState.OPEN.value:
            continue
        if order.get("action") != "buy":
            continue
        notional = _decimal(order.get("model_limit_price")) * int(order.get("qty") or 0)
        fee = fee_model.quote(
            action="buy", asset_type=str(order.get("asset_type") or "股票"), notional=notional
        ).total
        reserved += notional + fee
    return _money(max(Decimal("0"), _money(state.get("cash")) - reserved))


def _available_position(
    state: Mapping[str, Any], orders: Mapping[str, Mapping[str, Any]], symbol: str,
    *, excluding_instruction: str | None = None,
) -> int:
    raw = (state.get("positions") or {}).get(symbol) or {}
    sellable_qty = min(
        int(raw.get("qty") or 0),
        max(0, int(raw.get("sellable_qty") or 0)),
    )
    reserved = sum(
        int(order.get("qty") or 0)
        for code, order in orders.items()
        if code != excluding_instruction
        and order.get("state") == SyntheticOrderState.OPEN.value
        and order.get("action") == "sell"
        and order.get("symbol") == symbol
    )
    return max(0, sellable_qty - reserved)


class _LedgerTransaction:
    def __init__(
        self,
        ledger: "ExperimentLedger",
        events: list[dict[str, Any]],
        state: dict[str, Any],
        orders: dict[str, dict[str, Any]],
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.state = state
        self.orders = orders

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        problems: list[str] = []
        missing = sorted(_REQUIRED_EVENT_FIELDS - set(payload))
        if missing:
            problems.append("缺少字段:" + ",".join(missing))
        if str(payload.get("experiment_id") or "") != EXPERIMENT_ID:
            problems.append("experiment_id 不匹配")
        code = str(payload.get("instruction_code") or "").strip()
        if not code:
            problems.append("instruction_code 为空")
        try:
            datetime.fromisoformat(str(payload.get("timestamp") or ""))
        except ValueError:
            problems.append("timestamp 不是合法 ISO 时间")
        problems.extend(
            f"机密不得入档 —— {finding}" for finding in archive.scan_for_secrets(payload)
        )
        if problems:
            raise ExperimentLedgerError("experiment event 被拒:" + ";".join(problems))

        sequence = len(self.events) + 1
        event_id = uuid.uuid4().hex
        event = {
            **copy.deepcopy(dict(payload)),
            "event_id": event_id,
            "sequence": sequence,
        }
        state_name = re.sub(r"[^a-z0-9_-]+", "-", str(event["event_type"]).lower())
        target = self.ledger.directory / f"{sequence:08d}-{state_name}-{event_id}.json"
        archive._write_json_durable(target, event)
        self.events.append(event)
        self.state = copy.deepcopy(event["account_state"])
        self.orders = copy.deepcopy(event["orders_state"])
        return event


@dataclass
class ExperimentLedger:
    root: Path
    initial_cash: Decimal = Decimal("1000")
    fee_model: SimulationFeeModel = field(default_factory=SimulationFeeModel)
    benchmark: SimulationBenchmarkConfig = field(default_factory=SimulationBenchmarkConfig)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.initial_cash = _money(self.initial_cash)

    @property
    def directory(self) -> Path:
        return self.root / "_experiment" / "ledger"

    def _read_events_unlocked(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        events: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExperimentLedgerError(f"experiment ledger event 无法读取:{path.name}") from exc
            expected = len(events) + 1
            if int(event.get("sequence") or 0) != expected:
                raise ExperimentLedgerError(
                    f"experiment ledger sequence 不连续:{path.name} expected={expected}"
                )
            if str(event.get("experiment_id") or "") != EXPERIMENT_ID:
                raise ExperimentLedgerError(f"experiment ledger identity mismatch:{path.name}")
            events.append(event)
        return events

    def _state_from_events(
        self, events: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if not events:
            return _empty_state(self.initial_cash, self.benchmark, started_at=None), {}
        latest = events[-1]
        state = latest.get("account_state")
        orders = latest.get("orders_state")
        if not isinstance(state, Mapping) or not isinstance(orders, Mapping):
            raise ExperimentLedgerError("experiment ledger latest fact 缺少完整 state")
        if _money(state.get("initial_cash")) != self.initial_cash:
            raise ExperimentLedgerError("existing experiment initial cash 与配置不一致")
        return copy.deepcopy(dict(state)), {
            str(code): copy.deepcopy(dict(order))
            for code, order in orders.items() if isinstance(order, Mapping)
        }

    @contextmanager
    def _transaction(self) -> Iterator[_LedgerTransaction]:
        archive._mkdirs_durable(self.directory)
        lock_path = self.directory / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                events = self._read_events_unlocked()
                state, orders = self._state_from_events(events)
                yield _LedgerTransaction(self, events, state, orders)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, *, at: datetime) -> dict[str, Any]:
        with self._transaction() as tx:
            if tx.events:
                return copy.deepcopy(tx.state)
            state = _empty_state(self.initial_cash, self.benchmark, started_at=at)
            event = _event(
                event_type="INITIALIZED", strategy_id="experiment",
                instruction_code=INITIAL_INSTRUCTION_CODE, object_id="", action="initialize",
                qty=0, model_limit_price=None, execution_price=None,
                order_state="INITIALIZED", fill_qty=0, fee=Decimal("0"),
                cash_delta=Decimal("0"), position_delta=0,
                realized_delta=Decimal("0"), resulting_position=None,
                at=at, state=state, orders={}, extra={
                    "initial_cash": _text(self.initial_cash),
                    "fee_model": self.fee_model.config.as_entry(),
                    "benchmark_config": {
                        "buy_and_hold_symbol": self.benchmark.buy_and_hold_symbol,
                    },
                },
            )
            tx.append(event)
            return copy.deepcopy(state)

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.directory.exists():
            return
        with self._transaction() as tx:
            copied = copy.deepcopy(tx.events)
        yield from copied

    def _read(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if not self.directory.exists():
            return self._state_from_events(())
        with self._transaction() as tx:
            return copy.deepcopy(tx.state), copy.deepcopy(tx.orders)

    def summary(self) -> dict[str, Any]:
        state, orders = self._read()
        available = _available_cash(state, orders, self.fee_model)
        positions = []
        for symbol, raw in sorted((state.get("positions") or {}).items()):
            positions.append({
                **copy.deepcopy(raw),
                "available_qty": _available_position(state, orders, symbol),
            })
        open_orders = [
            _public_order(order)
            for _, order in sorted(orders.items())
            if order.get("state") == SyntheticOrderState.OPEN.value
        ]
        return {
            "experiment_id": EXPERIMENT_ID,
            "initial_cash": _text(state.get("initial_cash")),
            "current_cash": _text(state.get("cash")),
            "available_cash": _text(available),
            "nav": _text(state.get("nav")),
            "realized_pnl": _text(state.get("realized_pnl")),
            "unrealized_pnl": _text(state.get("unrealized_pnl")),
            "market_value": _text(state.get("market_value")),
            "cumulative_fees": _text(state.get("cumulative_fees")),
            "turnover": _text(state.get("turnover")),
            "high_water_mark": _text(state.get("high_water_mark")),
            "drawdown_pct": _text(state.get("current_drawdown_pct")),
            "max_drawdown_pct": _text(state.get("max_drawdown_pct")),
            "positions": positions,
            "open_synthetic_orders": open_orders,
            "last_mark_time": state.get("last_mark_at"),
            "experiment_start_time": state.get("started_at"),
            "benchmarks": copy.deepcopy(state.get("benchmark") or {}),
            "fee_model": self.fee_model.config.as_entry(),
        }

    def experiment_snapshot(self) -> experiment.ExperimentSnapshot:
        state, orders = self._read()
        exposures: dict[str, Decimal] = {}
        positions: dict[str, int] = {}
        for symbol, raw in (state.get("positions") or {}).items():
            positions[str(symbol)] = int(raw.get("qty") or 0)
            exposures[str(symbol)] = _money(raw.get("market_value"))
        for order in orders.values():
            if order.get("state") != SyntheticOrderState.OPEN.value or order.get("action") != "buy":
                continue
            symbol = str(order.get("symbol") or "")
            exposures[symbol] = exposures.get(symbol, Decimal("0")) + _money(
                _decimal(order.get("model_limit_price")) * int(order.get("qty") or 0)
            )
        return experiment.ExperimentSnapshot(
            net_equity_cny=_money(state.get("nav")),
            available_cash_cny=_available_cash(state, orders, self.fee_model),
            deployed_capital_cny=_money(sum(exposures.values(), Decimal("0"))),
            symbol_exposure_cny=exposures,
            symbol_position_qty=positions,
        )

    def model_account_context(self, *, now: datetime) -> dict[str, Any]:
        summary = self.summary()
        positions = [
            {
                "证券代码": row["symbol"],
                "证券名称": row["name"],
                "持仓数量": row["qty"],
                "可用数量": row["available_qty"],
                "冻结数量": row["qty"] - row["available_qty"],
                "成本价": row["average_cost"],
                "市值": row["market_value"],
                "浮动盈亏": row["unrealized_pnl"],
            }
            for row in summary["positions"]
        ]
        activity = []
        for event in self.iter_events():
            stamp = _iso(event.get("timestamp"))
            if stamp is None or stamp.date() != now.date():
                continue
            if event.get("action") not in {"buy", "sell", "cancel"}:
                continue
            activity.append({
                "instruction_code": event.get("instruction_code"),
                "委托编号": event.get("order_reference"),
                "代码": event.get("symbol"),
                "方向": event.get("action"),
                "委托数量": event.get("qty"),
                "委托价格": event.get("model_limit_price"),
                "成交数量": event.get("fill_qty"),
                "成交价格": event.get("simulated_execution_price"),
                "状态": event.get("order_state"),
                "时间": event.get("timestamp"),
            })
        return {
            "取到了": True,
            "来源": "ExperimentLedger isolated synthetic account",
            "账户": {
                "初始资金": summary["initial_cash"],
                "总资产": summary["nav"],
                "资金余额": summary["current_cash"],
                "可用资金": summary["available_cash"],
                "证券市值": summary["market_value"],
                "累计已实现盈亏": summary["realized_pnl"],
                "浮动盈亏": summary["unrealized_pnl"],
                "累计费用": summary["cumulative_fees"],
                "最大回撤": summary["max_drawdown_pct"],
                "持仓列表": positions,
            },
            "当日流水": {"取到了": True, "条数": len(activity), "明细": activity[-100:]},
        }

    def model_position_context(self, symbol: str) -> dict[str, Any]:
        state, orders = self._read()
        position = (state.get("positions") or {}).get(symbol)
        if not isinstance(position, Mapping):
            return {
                "持有": False, "数量": 0, "可用数量": 0, "冻结数量": 0,
                "成本价": None, "市值": "0.00", "浮动盈亏": "0.00", "当日盈亏": None,
            }
        available = _available_position(state, orders, symbol)
        qty = int(position.get("qty") or 0)
        return {
            "持有": qty > 0,
            "数量": qty,
            "可用数量": available,
            "冻结数量": qty - available,
            "成本价": position.get("average_cost"),
            "市值": position.get("market_value"),
            "浮动盈亏": position.get("unrealized_pnl"),
            "当日盈亏": None,
        }


def _event(
    *,
    event_type: str,
    strategy_id: str,
    instruction_code: str,
    object_id: str,
    action: str,
    qty: int,
    model_limit_price: Decimal | None,
    execution_price: Decimal | None,
    order_state: str,
    fill_qty: int,
    fee: Decimal,
    cash_delta: Decimal,
    position_delta: int,
    realized_delta: Decimal,
    resulting_position: Mapping[str, Any] | None,
    at: datetime,
    state: Mapping[str, Any],
    orders: Mapping[str, Mapping[str, Any]],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "event_type": event_type,
        "strategy_id": strategy_id,
        "instruction_code": instruction_code,
        "object_id": object_id,
        "action": action,
        "qty": qty,
        "model_limit_price": _text(model_limit_price) if model_limit_price is not None else None,
        "simulated_execution_price": _text(execution_price) if execution_price is not None else None,
        "order_state": order_state,
        "fill_qty": fill_qty,
        "fee": _text(_money(fee)),
        "cash_delta": _text(_money(cash_delta)),
        "position_delta": position_delta,
        "realized_pnl_delta": _text(_money(realized_delta)),
        "resulting_cash": _text(state.get("cash")),
        "resulting_position": copy.deepcopy(resulting_position),
        "resulting_nav": _text(state.get("nav")),
        "timestamp": at.isoformat(),
        "account_state": copy.deepcopy(dict(state)),
        "orders_state": copy.deepcopy(dict(orders)),
        **copy.deepcopy(dict(extra or {})),
    }


def _public_order(order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(order.get(key))
        for key in (
            "strategy_id", "instruction_code", "order_reference", "object_id", "market",
            "symbol", "name", "asset_type", "action", "qty", "model_limit_price",
            "turnover_mode",
            "simulated_execution_price", "state", "fill_qty", "fee", "created_at",
            "updated_at", "expires_at", "reasons", "target_order_reference",
        )
    }


def _order_reference(instruction_code: str) -> str:
    digest = hashlib.sha256(instruction_code.encode("utf-8")).hexdigest()[:20]
    return f"paper-{digest}"


@dataclass
class PaperExecutionEngine:
    ledger: ExperimentLedger
    rules: SimulationVenueRules = field(default_factory=SimulationVenueRules)

    @property
    def fee_model(self) -> SimulationFeeModel:
        return self.ledger.fee_model

    def quote_references(
        self,
        catalog: Catalog,
        snapshots: Mapping[str, ObjectSnapshot],
        *,
        observed_at: datetime,
    ) -> dict[str, QuoteReference]:
        refs: dict[str, QuoteReference] = {}
        for obj in catalog.tradable:
            snap = snapshots.get(obj.symbol)
            price = snap.last_price if snap is not None else Decimal("0")
            usable = bool(
                snap is not None
                and snap.quote_is_today
                and price.is_finite()
                and price > 0
            )
            refs[obj.symbol] = QuoteReference(
                object_id=obj.object_id,
                symbol=obj.symbol,
                price=price,
                observed_at=observed_at,
                usable=usable,
            )
        return refs

    def prepare_round(
        self,
        *,
        strategy_id: str,
        catalog: Catalog,
        snapshots: Mapping[str, ObjectSnapshot],
        now: datetime,
    ) -> Mapping[str, QuoteReference]:
        self.ledger.initialize(at=now)
        self.expire_due(now=now)
        self.release_due_settlements(strategy_id=strategy_id, now=now)
        proposed = self.quote_references(catalog, snapshots, observed_at=now)
        references = self._observe_market(strategy_id=strategy_id, references=proposed, now=now)
        self._fill_open_orders(catalog=catalog, references=references, now=now)
        return references

    def release_due_settlements(self, *, strategy_id: str, now: datetime) -> int:
        """Release prior-trading-day BUY lots at this valid round observation.

        A calendar day alone never advances settlement.  The release is attempted only while an
        actual simulation round is being prepared, and the existing trading calendar must confirm
        that the observed day is open.  The resulting complete state is one durable ledger fact.
        """
        if not tradingdays.is_trading_day(now.date()):
            return 0

        instruction_code = f"experiment:settlement:{now.date().isoformat()}"
        with self.ledger._transaction() as tx:
            if any(
                event.get("instruction_code") == instruction_code
                and event.get("event_type") == "SETTLEMENT_RELEASED"
                for event in tx.events
            ):
                return 0

            state = copy.deepcopy(tx.state)
            releases: list[dict[str, Any]] = []
            for symbol, position in sorted((state.get("positions") or {}).items()):
                pending = position.get("pending_settlements") or []
                remaining: list[dict[str, Any]] = []
                released_qty = 0
                released_lots: list[dict[str, Any]] = []
                for raw in pending:
                    lot = copy.deepcopy(dict(raw))
                    try:
                        trade_date = date.fromisoformat(str(lot.get("trade_date") or ""))
                    except ValueError as exc:
                        raise ExperimentLedgerError(
                            f"pending settlement 缺少合法 trade_date:{symbol}"
                        ) from exc
                    if trade_date < now.date():
                        released_qty += int(lot.get("qty") or 0)
                        released_lots.append(lot)
                    else:
                        remaining.append(lot)
                if released_qty <= 0:
                    continue
                position["sellable_qty"] = int(position.get("sellable_qty") or 0) + released_qty
                position["pending_settlements"] = remaining
                position["pending_settlement_qty"] = sum(
                    int(item.get("qty") or 0) for item in remaining
                )
                releases.append({
                    "object_id": position.get("object_id"),
                    "symbol": symbol,
                    "qty": released_qty,
                    "lots": released_lots,
                    "resulting_sellable_qty": position["sellable_qty"],
                    "resulting_pending_settlement_qty": position["pending_settlement_qty"],
                })

            if not releases:
                return 0

            _revalue(state)
            tx.append(_event(
                event_type="SETTLEMENT_RELEASED",
                strategy_id=strategy_id,
                instruction_code=instruction_code,
                object_id="",
                action="settle",
                qty=sum(int(item["qty"]) for item in releases),
                model_limit_price=None,
                execution_price=None,
                order_state="SETTLED",
                fill_qty=0,
                fee=Decimal("0"),
                cash_delta=Decimal("0"),
                position_delta=0,
                realized_delta=Decimal("0"),
                resulting_position=None,
                at=now,
                state=state,
                orders=tx.orders,
                extra={
                    "settlement_date": now.date().isoformat(),
                    "settlement_releases": releases,
                    "settlement_model": "T+1 releases at first observation on a later valid trading day",
                },
            ))
            return sum(int(item["qty"]) for item in releases)

    def _observe_market(
        self,
        *,
        strategy_id: str,
        references: Mapping[str, QuoteReference],
        now: datetime,
    ) -> dict[str, QuoteReference]:
        code = f"{strategy_id}:market-observation"
        with self.ledger._transaction() as tx:
            for prior in tx.events:
                if prior.get("instruction_code") == code and prior.get("event_type") == "MARKED":
                    raw = prior.get("market_references") or {}
                    return {
                        str(symbol): QuoteReference.from_entry(entry)
                        for symbol, entry in raw.items() if isinstance(entry, Mapping)
                    }
            state = copy.deepcopy(tx.state)
            for symbol, ref in references.items():
                if ref.usable:
                    state.setdefault("last_prices", {})[symbol] = _text(ref.price)
            benchmark = state.setdefault("benchmark", {})
            bench_symbol = benchmark.get("buy_and_hold_symbol")
            if bench_symbol and bench_symbol in references and references[bench_symbol].usable:
                price = references[bench_symbol].price
                if benchmark.get("initial_price") is None:
                    benchmark["initial_price"] = _text(price)
                    units = self.ledger.initial_cash / price
                    benchmark["units"] = _text(units)
                benchmark["last_price"] = _text(price)
                benchmark["nav"] = _text(_money(_decimal(benchmark.get("units")) * price))
            _revalue(state, mark_at=now)
            event = _event(
                event_type="MARKED", strategy_id=strategy_id,
                instruction_code=code, object_id="", action="mark",
                qty=0, model_limit_price=None, execution_price=None,
                order_state="MARKED", fill_qty=0, fee=Decimal("0"),
                cash_delta=Decimal("0"), position_delta=0,
                realized_delta=Decimal("0"), resulting_position=None,
                at=now, state=state, orders=tx.orders,
                extra={
                    "market_references": {
                        symbol: ref.as_entry() for symbol, ref in references.items()
                    },
                    "cash_benchmark_nav": _text(self.ledger.initial_cash),
                    "buy_and_hold_benchmark": copy.deepcopy(benchmark),
                },
            )
            tx.append(event)
        return dict(references)

    def submit(
        self,
        order: ValidatedOrder,
        *,
        strategy_id: str,
        object_id: str,
        catalog: Catalog,
        references: Mapping[str, QuoteReference],
        now: datetime,
    ) -> SyntheticExecutionResult:
        if order.action == "cancel":
            return self._cancel(order, strategy_id=strategy_id, object_id=object_id, now=now)
        instrument = catalog.get(object_id)
        with self.ledger._transaction() as tx:
            existing = tx.orders.get(order.instruction_code)
            if existing is not None:
                return SyntheticExecutionResult.from_order(existing)
            available_cash = _available_cash(tx.state, tx.orders, self.fee_model)
            available_qty = _available_position(tx.state, tx.orders, order.symbol)
            reasons = list(self.rules.validate(
                order, instrument, available_cash=available_cash,
                available_position_qty=available_qty, fee_model=self.fee_model,
            ))
            reference = references.get(order.symbol)
            if reference is None or not reference.usable:
                reasons.append({
                    "code": "MARKET_REFERENCE_UNAVAILABLE",
                    "message": "本轮没有可用的当时行情 reference，synthetic venue fail closed。",
                })
            if reasons:
                rejected = self._new_order(
                    order, strategy_id=strategy_id, object_id=object_id,
                    instrument=instrument, state=SyntheticOrderState.REJECTED,
                    now=now, reasons=reasons,
                )
                tx.orders[order.instruction_code] = rejected
                event = _event_for_order(
                    "ORDER_REJECTED", rejected, tx.state, tx.orders, at=now,
                    cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
                )
                tx.append(event)
                return SyntheticExecutionResult.from_order(rejected)

            assert instrument is not None and reference is not None
            marketable = (
                order.limit_price >= reference.price
                if order.action == "buy"
                else order.limit_price <= reference.price
            )
            opened = self._new_order(
                order, strategy_id=strategy_id, object_id=object_id,
                instrument=instrument, state=SyntheticOrderState.OPEN,
                now=now, reasons=(),
            )
            tx.orders[order.instruction_code] = opened
            if marketable:
                return self._fill_locked(
                    tx, opened, instrument=instrument, reference=reference, now=now
                )
            event = _event_for_order(
                "ORDER_OPENED", opened, tx.state, tx.orders, at=now,
                cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
            )
            tx.append(event)
            return SyntheticExecutionResult.from_order(opened)

    def _new_order(
        self,
        order: ValidatedOrder,
        *,
        strategy_id: str,
        object_id: str,
        instrument: TradeObject | None,
        state: SyntheticOrderState,
        now: datetime,
        reasons: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "instruction_code": order.instruction_code,
            "order_reference": _order_reference(order.instruction_code),
            "object_id": object_id,
            "market": order.market,
            "symbol": order.symbol,
            "name": order.name,
            "asset_type": instrument.asset_type if instrument is not None else "",
            "lot_size": instrument.lot_size if instrument is not None else 0,
            "turnover_mode": instrument.turnover_mode if instrument is not None else "",
            "action": order.action,
            "qty": order.qty,
            "model_limit_price": _text(order.limit_price),
            "simulated_execution_price": None,
            "state": state.value,
            "fill_qty": 0,
            "fee": "0.00",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": _expires_at(now).isoformat(),
            "reasons": [dict(reason) for reason in reasons],
            "target_order_reference": order.wtbh,
        }

    def _fill_locked(
        self,
        tx: _LedgerTransaction,
        order: dict[str, Any],
        *,
        instrument: TradeObject,
        reference: QuoteReference,
        now: datetime,
    ) -> SyntheticExecutionResult:
        code = str(order["instruction_code"])
        if order.get("state") != SyntheticOrderState.OPEN.value:
            return SyntheticExecutionResult.from_order(order)
        qty = int(order["qty"])
        action = str(order["action"])
        price = reference.price
        notional = _money(price * qty)
        fee_quote = self.fee_model.quote(
            action=action, asset_type=instrument.asset_type, notional=notional
        )
        fee = fee_quote.total
        state = copy.deepcopy(tx.state)
        orders = copy.deepcopy(tx.orders)
        cash_before = _money(state.get("cash"))
        realized_delta = Decimal("0")
        position_delta = qty if action == "buy" else -qty
        positions = state.setdefault("positions", {})
        prior = positions.get(instrument.symbol)

        if action == "buy":
            required = _money(notional + fee)
            available = _available_cash(
                state, orders, self.fee_model, excluding_instruction=code
            )
            if required > available:
                return self._reject_open_locked(
                    tx, order, now=now, code="INSUFFICIENT_SYNTHETIC_CASH_AT_FILL",
                    message=f"触价时需要 {required} CNY，可用 synthetic cash 为 {available} CNY。",
                )
            prior_qty = int((prior or {}).get("qty") or 0)
            prior_sellable = int((prior or {}).get("sellable_qty") or 0)
            pending_settlements = [
                copy.deepcopy(dict(item))
                for item in ((prior or {}).get("pending_settlements") or [])
            ]
            prior_basis = _decimal((prior or {}).get("average_cost")) * prior_qty
            new_qty = prior_qty + qty
            new_avg = (prior_basis + notional + fee) / new_qty
            if self.rules.buy_is_immediately_sellable(instrument):
                new_sellable = prior_sellable + qty
            else:
                new_sellable = prior_sellable
                pending_settlements.append({
                    "instruction_code": code,
                    "trade_date": now.date().isoformat(),
                    "qty": qty,
                    "turnover_mode": TURNOVER_T1,
                })
            state["cash"] = _text(_money(cash_before - required))
            positions[instrument.symbol] = _position_entry(
                instrument=instrument,
                qty=new_qty,
                sellable_qty=new_sellable,
                pending_settlements=pending_settlements,
                avg_cost=new_avg,
                last_price=price,
            )
            cash_delta = -required
        else:
            available_qty = _available_position(
                state, orders, instrument.symbol, excluding_instruction=code
            )
            if prior is None or qty > available_qty:
                return self._reject_open_locked(
                    tx, order, now=now, code="OVERSELL_AT_FILL",
                    message=f"触价时拟卖出 {qty}，synthetic 可卖数量为 {available_qty}。",
                )
            prior_qty = int(prior.get("qty") or 0)
            prior_sellable = int(prior.get("sellable_qty") or 0)
            pending_settlements = [
                copy.deepcopy(dict(item))
                for item in (prior.get("pending_settlements") or [])
            ]
            avg_cost = _decimal(prior.get("average_cost"))
            proceeds = _money(notional - fee)
            realized_delta = _money(proceeds - avg_cost * qty)
            new_qty = prior_qty - qty
            state["cash"] = _text(_money(cash_before + proceeds))
            if new_qty:
                positions[instrument.symbol] = _position_entry(
                    instrument=instrument,
                    qty=new_qty,
                    sellable_qty=prior_sellable - qty,
                    pending_settlements=pending_settlements,
                    avg_cost=avg_cost,
                    last_price=price,
                )
            else:
                positions.pop(instrument.symbol, None)
            cash_delta = proceeds

        state.setdefault("last_prices", {})[instrument.symbol] = _text(price)
        state["realized_pnl"] = _text(_money(
            _decimal(state.get("realized_pnl")) + realized_delta
        ))
        state["cumulative_fees"] = _text(_money(
            _decimal(state.get("cumulative_fees")) + fee
        ))
        state["turnover"] = _text(_money(_decimal(state.get("turnover")) + notional))
        filled = copy.deepcopy(order)
        filled.update({
            "state": SyntheticOrderState.FILLED.value,
            "fill_qty": qty,
            "simulated_execution_price": _text(price),
            "fee": _text(fee),
            "fee_breakdown": fee_quote.as_entry(),
            "updated_at": now.isoformat(),
        })
        orders[code] = filled
        _revalue(state, mark_at=now)
        resulting = (state.get("positions") or {}).get(instrument.symbol)
        event = _event_for_order(
            "ORDER_FILLED", filled, state, orders, at=now,
            cash_delta=cash_delta, position_delta=position_delta,
            realized_delta=realized_delta, resulting_position=resulting,
            extra={
                "simulated_notional": _text(notional),
                "fee_breakdown": fee_quote.as_entry(),
                "market_reference": reference.as_entry(),
                "turnover_mode": instrument.turnover_mode,
                "fill_model": "marketable DAY limit fills once at current observed reference price",
            },
        )
        tx.append(event)
        return SyntheticExecutionResult.from_order(filled)

    def _reject_open_locked(
        self,
        tx: _LedgerTransaction,
        order: dict[str, Any],
        *,
        now: datetime,
        code: str,
        message: str,
    ) -> SyntheticExecutionResult:
        rejected = copy.deepcopy(order)
        rejected.update({
            "state": SyntheticOrderState.REJECTED.value,
            "updated_at": now.isoformat(),
            "reasons": [*list(rejected.get("reasons") or ()), {"code": code, "message": message}],
        })
        orders = copy.deepcopy(tx.orders)
        orders[str(order["instruction_code"])] = rejected
        event = _event_for_order(
            "ORDER_REJECTED", rejected, tx.state, orders, at=now,
            cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
        )
        tx.append(event)
        return SyntheticExecutionResult.from_order(rejected)

    def _fill_open_orders(
        self,
        *,
        catalog: Catalog,
        references: Mapping[str, QuoteReference],
        now: datetime,
    ) -> None:
        while True:
            with self.ledger._transaction() as tx:
                candidate: tuple[dict[str, Any], TradeObject, QuoteReference] | None = None
                for code, order in sorted(tx.orders.items()):
                    if order.get("state") != SyntheticOrderState.OPEN.value:
                        continue
                    instrument = catalog.get(str(order.get("object_id") or ""))
                    reference = references.get(str(order.get("symbol") or ""))
                    if instrument is None or reference is None or not reference.usable:
                        continue
                    limit_price = _decimal(order.get("model_limit_price"))
                    marketable = (
                        limit_price >= reference.price
                        if order.get("action") == "buy"
                        else limit_price <= reference.price
                    )
                    if marketable:
                        candidate = (order, instrument, reference)
                        break
                if candidate is None:
                    return
                self._fill_locked(
                    tx, candidate[0], instrument=candidate[1], reference=candidate[2], now=now
                )

    def expire_due(self, *, now: datetime) -> int:
        expired = 0
        while True:
            with self.ledger._transaction() as tx:
                target: dict[str, Any] | None = None
                for _, order in sorted(tx.orders.items()):
                    if order.get("state") != SyntheticOrderState.OPEN.value:
                        continue
                    expiry = _iso(order.get("expires_at"))
                    if expiry is not None and expiry <= now:
                        target = order
                        break
                if target is None:
                    return expired
                closed = copy.deepcopy(target)
                closed.update({
                    "state": SyntheticOrderState.EXPIRED.value,
                    "updated_at": now.isoformat(),
                })
                orders = copy.deepcopy(tx.orders)
                orders[str(closed["instruction_code"])] = closed
                event = _event_for_order(
                    "ORDER_EXPIRED", closed, tx.state, orders, at=now,
                    cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
                )
                tx.append(event)
                expired += 1

    def _cancel(
        self,
        order: ValidatedOrder,
        *,
        strategy_id: str,
        object_id: str,
        now: datetime,
    ) -> SyntheticExecutionResult:
        with self.ledger._transaction() as tx:
            existing = tx.orders.get(order.instruction_code)
            if existing is not None:
                return SyntheticExecutionResult.from_order(existing)
            target_ref = str(order.wtbh or "")
            target_code = next((
                code for code, candidate in tx.orders.items()
                if candidate.get("order_reference") == target_ref
            ), None)
            target = tx.orders.get(target_code or "")
            if target is None or target.get("state") != SyntheticOrderState.OPEN.value:
                cancel = self._new_order(
                    order, strategy_id=strategy_id, object_id=object_id,
                    instrument=None, state=SyntheticOrderState.REJECTED, now=now,
                    reasons=({
                        "code": "SYNTHETIC_ORDER_NOT_OPEN",
                        "message": "目标 synthetic order 不存在或已经不是 OPEN。",
                    },),
                )
                tx.orders[order.instruction_code] = cancel
                tx.append(_event_for_order(
                    "CANCEL_REJECTED", cancel, tx.state, tx.orders, at=now,
                    cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
                ))
                return SyntheticExecutionResult.from_order(cancel)

            orders = copy.deepcopy(tx.orders)
            cancelled_target = copy.deepcopy(target)
            cancelled_target.update({
                "state": SyntheticOrderState.CANCELLED.value,
                "updated_at": now.isoformat(),
            })
            orders[str(target_code)] = cancelled_target
            cancel = self._new_order(
                order, strategy_id=strategy_id, object_id=object_id,
                instrument=None, state=SyntheticOrderState.CANCELLED, now=now, reasons=(),
            )
            cancel["order_reference"] = target_ref
            cancel["target_instruction_code"] = target_code
            orders[order.instruction_code] = cancel
            event = _event_for_order(
                "ORDER_CANCELLED", cancel, tx.state, orders, at=now,
                cash_delta=Decimal("0"), position_delta=0, realized_delta=Decimal("0"),
                extra={"cancelled_instruction_code": target_code},
            )
            tx.append(event)
            return SyntheticExecutionResult.from_order(cancel)


def _event_for_order(
    event_type: str,
    order: Mapping[str, Any],
    state: Mapping[str, Any],
    orders: Mapping[str, Mapping[str, Any]],
    *,
    at: datetime,
    cash_delta: Decimal,
    position_delta: int,
    realized_delta: Decimal,
    resulting_position: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    price = order.get("simulated_execution_price")
    return _event(
        event_type=event_type,
        strategy_id=str(order.get("strategy_id") or ""),
        instruction_code=str(order.get("instruction_code") or ""),
        object_id=str(order.get("object_id") or ""),
        action=str(order.get("action") or ""),
        qty=int(order.get("qty") or 0),
        model_limit_price=_decimal(order.get("model_limit_price"))
        if order.get("model_limit_price") is not None else None,
        execution_price=_decimal(price) if price is not None else None,
        order_state=str(order.get("state") or ""),
        fill_qty=int(order.get("fill_qty") or 0),
        fee=_money(order.get("fee")),
        cash_delta=cash_delta,
        position_delta=position_delta,
        realized_delta=realized_delta,
        resulting_position=resulting_position,
        at=at,
        state=state,
        orders=orders,
        extra={
            "order_reference": order.get("order_reference"),
            "market": order.get("market"),
            "symbol": order.get("symbol"),
            "name": order.get("name"),
            "asset_type": order.get("asset_type"),
            "turnover_mode": order.get("turnover_mode"),
            "reasons": copy.deepcopy(order.get("reasons") or []),
            **dict(extra or {}),
        },
    )


__all__ = [
    "EXPERIMENT_ID", "ExperimentLedgerError", "SyntheticOrderState",
    "TERMINAL_ORDER_STATES", "SimulationFeeConfig", "FeeQuote",
    "SimulationFeeModel", "SimulationBenchmarkConfig", "QuoteReference",
    "SyntheticExecutionResult", "SimulationVenueRules", "ExperimentLedger",
    "PaperExecutionEngine",
]
