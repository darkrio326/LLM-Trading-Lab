"""Broker-agnostic experiment capital policy for the M0 simulation baseline.

The policy is deliberately separate from strategy generation and broker rules.  It sees the
model's normalized order and an experiment-only account snapshot, then returns exactly PASS or
REJECT.  It never changes quantity, price, side, or any other proposed field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from .guards import AccountSnapshot, ObjectSnapshot, ValidatedOrder


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


@dataclass(frozen=True)
class ExperimentPolicyConfig:
    """Owner-configured capital boundary.  Values are CNY unless noted otherwise."""

    initial_bankroll_cny: Decimal = Decimal("1000")
    max_deployed_capital_cny: Decimal = Decimal("900")
    min_cash_reserve_cny: Decimal = Decimal("100")
    max_single_order_cny: Decimal = Decimal("600")
    max_single_symbol_exposure_cny: Decimal = Decimal("600")
    max_drawdown_pct: Decimal = Decimal("25")

    @property
    def buy_lock_equity_cny(self) -> Decimal:
        return self.initial_bankroll_cny * (
            Decimal("1") - self.max_drawdown_pct / Decimal("100")
        )

    def as_entry(self) -> dict[str, str]:
        return {
            "initial_bankroll_cny": str(self.initial_bankroll_cny),
            "max_deployed_capital_cny": str(self.max_deployed_capital_cny),
            "min_cash_reserve_cny": str(self.min_cash_reserve_cny),
            "max_single_order_cny": str(self.max_single_order_cny),
            "max_single_symbol_exposure_cny": str(
                self.max_single_symbol_exposure_cny
            ),
            "max_drawdown_pct": str(self.max_drawdown_pct),
        }


@dataclass(frozen=True)
class ExperimentSnapshot:
    """Experiment allocation facts, independent of any broker-specific account type."""

    net_equity_cny: Decimal
    available_cash_cny: Decimal
    deployed_capital_cny: Decimal
    symbol_exposure_cny: Mapping[str, Decimal] = field(default_factory=dict)
    symbol_position_qty: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def initial(
        cls, config: ExperimentPolicyConfig | None = None
    ) -> "ExperimentSnapshot":
        cfg = config or ExperimentPolicyConfig()
        return cls(
            net_equity_cny=cfg.initial_bankroll_cny,
            available_cash_cny=cfg.initial_bankroll_cny,
            deployed_capital_cny=Decimal("0"),
        )

    def after(self, order: ValidatedOrder) -> "ExperimentSnapshot":
        """Return an immutable projected snapshot for later orders in the same batch."""
        return self.after_values(
            action=order.action,
            symbol=order.symbol,
            qty=order.qty,
            notional=order.notional,
        )

    def after_values(
        self, *, action: str, symbol: str, qty: int, notional: Decimal
    ) -> "ExperimentSnapshot":
        """Project durable order facts without reconstructing or changing the order."""
        exposures = dict(self.symbol_exposure_cny)
        positions = dict(self.symbol_position_qty)
        exposure = exposures.get(symbol, Decimal("0"))
        position = positions.get(symbol, 0)
        if action == "buy":
            exposures[symbol] = exposure + notional
            positions[symbol] = position + qty
            cash = self.available_cash_cny - notional
            deployed = self.deployed_capital_cny + notional
        elif action == "sell":
            exposures[symbol] = max(Decimal("0"), exposure - notional)
            positions[symbol] = max(0, position - qty)
            cash = self.available_cash_cny + notional
            deployed = max(Decimal("0"), self.deployed_capital_cny - notional)
        else:
            return self
        return ExperimentSnapshot(
            net_equity_cny=self.net_equity_cny,
            available_cash_cny=cash,
            deployed_capital_cny=deployed,
            symbol_exposure_cny=exposures,
            symbol_position_qty=positions,
        )

    def as_entry(self) -> dict[str, Any]:
        return {
            "net_equity_cny": str(self.net_equity_cny),
            "available_cash_cny": str(self.available_cash_cny),
            "deployed_capital_cny": str(self.deployed_capital_cny),
            "symbol_exposure_cny": {
                key: str(value) for key, value in self.symbol_exposure_cny.items()
            },
            "symbol_position_qty": dict(self.symbol_position_qty),
        }


class PolicyResult(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


@dataclass(frozen=True)
class PolicyReason:
    code: str
    message: str

    def as_entry(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class PolicyDecision:
    result: PolicyResult
    reasons: tuple[PolicyReason, ...] = ()

    @property
    def passed(self) -> bool:
        return self.result is PolicyResult.PASS


@dataclass(frozen=True)
class ExperimentPolicy:
    config: ExperimentPolicyConfig = field(default_factory=ExperimentPolicyConfig)

    def evaluate(
        self, order: ValidatedOrder, snapshot: ExperimentSnapshot
    ) -> PolicyDecision:
        """PASS or REJECT the exact order.  The returned order is never rewritten."""
        reasons: list[PolicyReason] = []

        if order.action == "cancel":
            return PolicyDecision(PolicyResult.PASS)

        if order.action == "sell":
            held = snapshot.symbol_position_qty.get(order.symbol, 0)
            if order.qty > held:
                reasons.append(
                    PolicyReason(
                        "SHORT_SELLING_FORBIDDEN",
                        f"拟卖出 {order.qty}，实验快照持有 {held}；禁止卖空。",
                    )
                )
            return PolicyDecision(
                PolicyResult.REJECT if reasons else PolicyResult.PASS,
                tuple(reasons),
            )

        if order.action != "buy":
            return PolicyDecision(
                PolicyResult.REJECT,
                (PolicyReason("UNSUPPORTED_ACTION", f"不支持的执行动作:{order.action}"),),
            )

        cfg = self.config
        if snapshot.net_equity_cny <= cfg.buy_lock_equity_cny:
            reasons.append(
                PolicyReason(
                    "EXPERIMENT_DRAWDOWN_BUY_LOCK",
                    f"实验净值 {snapshot.net_equity_cny} <= {cfg.buy_lock_equity_cny}，禁止新的 BUY。",
                )
            )
        if order.notional > cfg.max_single_order_cny:
            reasons.append(
                PolicyReason(
                    "MAX_SINGLE_ORDER_EXCEEDED",
                    f"拟下单金额 {order.notional} > 单笔上限 {cfg.max_single_order_cny}。",
                )
            )
        if snapshot.deployed_capital_cny + order.notional > cfg.max_deployed_capital_cny:
            reasons.append(
                PolicyReason(
                    "EXPERIMENT_BUDGET_EXCEEDED",
                    "拟下单后实验已部署资金将超过上限。",
                )
            )
        if order.notional > snapshot.available_cash_cny:
            reasons.append(
                PolicyReason(
                    "LEVERAGE_FORBIDDEN",
                    "拟下单金额超过实验可用现金；禁止杠杆或融资。",
                )
            )
        if snapshot.available_cash_cny - order.notional < cfg.min_cash_reserve_cny:
            reasons.append(
                PolicyReason(
                    "MIN_CASH_RESERVE_BREACHED",
                    f"拟下单后现金低于保留金 {cfg.min_cash_reserve_cny}。",
                )
            )
        symbol_exposure = snapshot.symbol_exposure_cny.get(order.symbol, Decimal("0"))
        if symbol_exposure + order.notional > cfg.max_single_symbol_exposure_cny:
            reasons.append(
                PolicyReason(
                    "MAX_SINGLE_SYMBOL_EXPOSURE_EXCEEDED",
                    "拟下单后单一标的敞口将超过实验上限。",
                )
            )

        return PolicyDecision(
            PolicyResult.REJECT if reasons else PolicyResult.PASS,
            tuple(reasons),
        )


def snapshot_from_validation_context(
    account: AccountSnapshot | None,
    objects: Mapping[str, ObjectSnapshot],
    *,
    config: ExperimentPolicyConfig | None = None,
) -> ExperimentSnapshot:
    """Build a conservative broker-neutral snapshot from an existing round input."""
    cfg = config or ExperimentPolicyConfig()
    exposures: dict[str, Decimal] = {}
    positions: dict[str, int] = {}
    for symbol, obj in objects.items():
        qty = max(0, int(obj.holding_qty or 0))
        exposure = max(Decimal("0"), Decimal(qty) * obj.last_price)
        exposures[str(symbol)] = exposure
        positions[str(symbol)] = qty
    deployed = sum(exposures.values(), Decimal("0"))
    cash = min(
        cfg.initial_bankroll_cny,
        account.available_cash if account is not None else cfg.initial_bankroll_cny,
    )
    equity = min(cfg.initial_bankroll_cny, max(Decimal("0"), cash + deployed))
    return ExperimentSnapshot(equity, cash, deployed, exposures, positions)


def snapshot_from_account_summary(
    raw: Mapping[str, Any] | None,
    *,
    config: ExperimentPolicyConfig | None = None,
) -> ExperimentSnapshot:
    """Build a snapshot from Store.account() without depending on a broker response type."""
    cfg = config or ExperimentPolicyConfig()
    if not isinstance(raw, Mapping):
        return ExperimentSnapshot.initial(cfg)
    account = raw.get("账户") if isinstance(raw.get("账户"), Mapping) else raw
    if not isinstance(account, Mapping):
        return ExperimentSnapshot.initial(cfg)

    exposures: dict[str, Decimal] = {}
    positions: dict[str, int] = {}
    rows = account.get("持仓列表")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("证券代码") or "").strip()
            if not symbol:
                continue
            qty = _int(row.get("持仓数量"))
            value = _decimal(row.get("市值"))
            positions[symbol] = max(0, qty or 0)
            exposures[symbol] = max(Decimal("0"), value or Decimal("0"))

    deployed_value = _decimal(account.get("证券市值"))
    deployed = (
        max(Decimal("0"), deployed_value)
        if deployed_value is not None
        else sum(exposures.values(), Decimal("0"))
    )
    available_value = _decimal(account.get("可用资金"))
    cash = min(
        cfg.initial_bankroll_cny,
        max(Decimal("0"), available_value)
        if available_value is not None
        else cfg.initial_bankroll_cny,
    )
    total_value = _decimal(account.get("总资产"))
    equity = min(
        cfg.initial_bankroll_cny,
        max(Decimal("0"), total_value)
        if total_value is not None
        else max(Decimal("0"), cash + deployed),
    )
    return ExperimentSnapshot(equity, cash, deployed, exposures, positions)


__all__ = [
    "ExperimentPolicyConfig", "ExperimentSnapshot", "PolicyResult", "PolicyReason",
    "PolicyDecision", "ExperimentPolicy", "snapshot_from_validation_context",
    "snapshot_from_account_summary",
]
