"""Broker-agnostic execution coordinator with durable at-most-once submission.

``instruction_code`` is the idempotency key. Every external BrokerAdapter write is preceded by
an fsynced EXECUTING fact in ``archive/_execution``. A recovered EXECUTING state is uncertain and
is moved to RECONCILE_REQUIRED; it is never retried automatically. This provides at-most-once
automatic submission and replay safety, not theoretical exactly-once delivery.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import SYSTEM_NAME, __version__, archive, experiment, runmode
from .broker import BrokerError
from .guards import GuardReport, ProposedOrder, ValidatedOrder

logger = logging.getLogger("zhixing.execution")


class AuthorizationKind(str, Enum):
    UNATTENDED = "unattended"
    MANUAL = "manual"
    SIMULATION = "simulation"


@dataclass(frozen=True)
class Authorization:
    kind: AuthorizationKind
    actor: str
    source: str
    issued_at: datetime


@dataclass(frozen=True)
class ExecutionMetadata:
    strategy_id: str = ""
    object_id: str = ""
    model: str = ""
    llm_provider: str = ""
    confidence: float | None = None


class ExecutionState(str, Enum):
    PREPARED = "PREPARED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    SUBMITTED = "SUBMITTED"
    SUBMITTED_UNKNOWN = "SUBMITTED_UNKNOWN"
    REJECTED = "REJECTED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    SIMULATED = "SIMULATED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class Outcome(str, Enum):
    SUBMITTED = "submitted"
    SUBMITTED_UNKNOWN = "submitted_unknown"
    DRY_RUN = "dry_run"
    REJECTED = "rejected"
    FAILED = "failed"
    RECONCILE_REQUIRED = "reconcile_required"


REPLAY_BLOCKING_STATES = frozenset({
    ExecutionState.SUBMITTED,
    ExecutionState.SUBMITTED_UNKNOWN,
    ExecutionState.REJECTED,
    ExecutionState.SIMULATED,
    ExecutionState.RECONCILE_REQUIRED,
})


@dataclass(frozen=True)
class ExecutionRecord:
    record_id: str
    system_name: str
    app_version: str
    strategy_id: str
    instruction_code: str
    object_id: str
    action: str
    market: str
    symbol: str
    name: str
    qty: int
    limit_price: Decimal
    notional: Decimal
    proposed_qty: Any
    proposed_limit_price: Any
    proposed_notional: Decimal | None
    outcome: Outcome
    execution_state: ExecutionState
    authorization: Authorization
    created_at: datetime
    attempted_at: datetime | None
    completed_at: datetime | None
    model: str = ""
    llm_provider: str = ""
    confidence: float | None = None
    guards_passed: tuple[str, ...] = ()
    broker_provider: str | None = None
    broker_receipt: Mapping[str, Any] | None = None
    wtbh: str | None = None
    message: str = ""
    submitted_unknown: bool = False
    experiment_policy_result: str = "PENDING"
    experiment_policy_reasons: tuple[Mapping[str, str], ...] = ()
    reason: str = ""
    risk_note: str = ""


class BrokerAdapter(Protocol):
    """Generic write boundary shared by every current or future broker integration.

    ``provider`` is a non-secret stable identity used for audit and reconciliation. Every broker
    needs this semantic; no URL, page, Selenium, or provider-specific field belongs here.
    """

    provider: str

    def place_order(self, order: ValidatedOrder) -> str:
        """Perform one broker submission attempt and return a broker order reference.

        The execution coordinator provides instruction_code-level at-most-once invocation and
        replay safety.
        """
        ...

    def cancel_order(self, wtbh: str) -> None:
        """Perform one broker cancellation attempt.

        The execution coordinator provides instruction_code-level at-most-once invocation and
        replay safety.
        """
        ...


record_sink: Callable[[ExecutionRecord], None] = lambda rec: logger.info(
    "执行留痕 %s %s %s %s qty=%s price=%s state=%s broker=%s",
    rec.record_id,
    rec.action,
    rec.market,
    rec.symbol,
    rec.qty,
    rec.limit_price,
    rec.execution_state.value,
    rec.broker_provider or "none",
)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _proposal_entry(proposed: ProposedOrder) -> dict[str, Any]:
    return {
        "instruction_code": proposed.instruction_code,
        "action": proposed.action,
        "market": proposed.market,
        "symbol": proposed.symbol,
        "name": proposed.name,
        "qty": _json_value(proposed.qty),
        "limit_price": _json_value(proposed.limit_price),
        "wtbh": proposed.wtbh,
        "reason": proposed.reason,
        "risk_note": proposed.risk_note,
    }


def _authorization_entry(auth: Authorization) -> dict[str, str]:
    return {
        "kind": auth.kind.value,
        "actor": auth.actor,
        "source": auth.source,
        "issued_at": auth.issued_at.isoformat(),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def record_entry(record: ExecutionRecord) -> dict[str, Any]:
    """Return the complete non-secret final execution fact for API and round archives."""
    proposed = {
        "instruction_code": record.instruction_code,
        "action": record.action,
        "market": record.market,
        "symbol": record.symbol,
        "name": record.name,
        "qty": _json_value(record.proposed_qty),
        "limit_price": _json_value(record.proposed_limit_price),
        "wtbh": record.wtbh if record.action == "cancel" else None,
        "reason": record.reason,
        "risk_note": record.risk_note,
    }
    return {
        "record_id": record.record_id,
        "system_name": record.system_name,
        "app_version": record.app_version,
        "strategy_id": record.strategy_id,
        "instruction_code": record.instruction_code,
        "object_id": record.object_id,
        "action": record.action,
        "market": record.market,
        "symbol": record.symbol,
        "name": record.name,
        "qty": record.qty,
        "limit_price": str(record.limit_price),
        "notional": str(record.notional),
        "proposed_order": proposed,
        "proposed_qty": _json_value(record.proposed_qty),
        "proposed_limit_price": _json_value(record.proposed_limit_price),
        "proposed_notional": (
            str(record.proposed_notional) if record.proposed_notional is not None else None
        ),
        "model": record.model,
        "llm_provider": record.llm_provider,
        "confidence": record.confidence,
        "experiment_policy_result": record.experiment_policy_result,
        "experiment_policy_reasons": [
            dict(reason) for reason in record.experiment_policy_reasons
        ],
        "authorization_kind": record.authorization.kind.value,
        "execution_state": record.execution_state.value,
        "broker_provider": record.broker_provider,
        "broker_receipt": (
            dict(record.broker_receipt) if record.broker_receipt is not None else None
        ),
        "broker_order_reference": record.wtbh,
        "outcome": record.outcome.value,
        "submitted_unknown": record.submitted_unknown,
        "created_at": record.created_at.isoformat(),
        "attempted_at": _iso(record.attempted_at),
        "completed_at": _iso(record.completed_at),
        "wtbh": record.wtbh,
        "message": record.message,
        "reason": record.reason,
        "risk_note": record.risk_note,
        "规范化步骤": list(record.guards_passed),
        "授权": _authorization_entry(record.authorization),
    }


def _outcome_for_state(state: ExecutionState) -> Outcome:
    if state is ExecutionState.SUBMITTED:
        return Outcome.SUBMITTED
    if state is ExecutionState.SUBMITTED_UNKNOWN:
        return Outcome.SUBMITTED_UNKNOWN
    if state is ExecutionState.SIMULATED:
        return Outcome.DRY_RUN
    if state in {ExecutionState.REJECTED, ExecutionState.NOT_AUTHORIZED}:
        return Outcome.REJECTED
    if state is ExecutionState.RECONCILE_REQUIRED:
        return Outcome.RECONCILE_REQUIRED
    return Outcome.FAILED


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


def _datetime(value: object, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return fallback


def _authorization_from_fact(fact: Mapping[str, Any], fallback: datetime) -> Authorization:
    raw = fact.get("授权")
    auth = raw if isinstance(raw, Mapping) else {}
    try:
        kind = AuthorizationKind(str(auth.get("kind") or fact.get("authorization_kind")))
    except ValueError:
        kind = AuthorizationKind.SIMULATION
    return Authorization(
        kind=kind,
        actor=str(auth.get("actor") or "unknown"),
        source=str(auth.get("source") or "journal"),
        issued_at=_datetime(auth.get("issued_at"), fallback),
    )


def _record_from_fact(fact: Mapping[str, Any], *, fallback_now: datetime) -> ExecutionRecord:
    state = ExecutionState(str(fact.get("execution_state") or ExecutionState.RECONCILE_REQUIRED.value))
    proposed = fact.get("proposed_order")
    raw = proposed if isinstance(proposed, Mapping) else {}
    attempted = fact.get("attempted_at")
    completed = fact.get("completed_at")
    policy_reasons = fact.get("experiment_policy_reasons")
    return ExecutionRecord(
        record_id=str(fact.get("record_id") or fact.get("event_id") or uuid.uuid4().hex),
        system_name=str(fact.get("system_name") or SYSTEM_NAME),
        app_version=str(fact.get("app_version") or __version__),
        strategy_id=str(fact.get("strategy_id") or ""),
        instruction_code=str(fact.get("instruction_code") or ""),
        object_id=str(fact.get("object_id") or ""),
        action=str(fact.get("action") or raw.get("action") or ""),
        market=str(fact.get("market") or raw.get("market") or ""),
        symbol=str(fact.get("symbol") or raw.get("symbol") or ""),
        name=str(fact.get("name") or raw.get("name") or ""),
        qty=int(fact.get("qty") or 0),
        limit_price=_decimal(fact.get("limit_price")),
        notional=_decimal(fact.get("notional")),
        proposed_qty=fact.get("proposed_qty", raw.get("qty")),
        proposed_limit_price=fact.get("proposed_limit_price", raw.get("limit_price")),
        proposed_notional=(
            _decimal(fact.get("proposed_notional"))
            if fact.get("proposed_notional") is not None
            else None
        ),
        outcome=_outcome_for_state(state),
        execution_state=state,
        authorization=_authorization_from_fact(fact, fallback_now),
        created_at=_datetime(fact.get("created_at"), fallback_now),
        attempted_at=_datetime(attempted, fallback_now) if attempted else None,
        completed_at=_datetime(completed, fallback_now) if completed else None,
        model=str(fact.get("model") or ""),
        llm_provider=str(fact.get("llm_provider") or ""),
        confidence=(
            float(fact["confidence"]) if isinstance(fact.get("confidence"), (int, float)) else None
        ),
        guards_passed=tuple(str(x) for x in (fact.get("规范化步骤") or ())),
        broker_provider=(
            str(fact.get("broker_provider")) if fact.get("broker_provider") else None
        ),
        broker_receipt=(
            dict(fact["broker_receipt"])
            if isinstance(fact.get("broker_receipt"), Mapping)
            else None
        ),
        wtbh=str(fact.get("broker_order_reference") or fact.get("wtbh") or "") or None,
        message=str(fact.get("message") or ""),
        submitted_unknown=bool(fact.get("submitted_unknown")),
        experiment_policy_result=str(fact.get("experiment_policy_result") or "PENDING"),
        experiment_policy_reasons=tuple(
            dict(reason) for reason in (policy_reasons or ()) if isinstance(reason, Mapping)
        ),
        reason=str(fact.get("reason") or raw.get("reason") or ""),
        risk_note=str(fact.get("risk_note") or raw.get("risk_note") or ""),
    )


def _authorization_denial(auth: Authorization) -> str | None:
    if not auth.actor.strip():
        return "授权缺少操作者标识"
    if not auth.source.strip():
        return "授权缺少触发来源"
    if auth.kind is AuthorizationKind.UNATTENDED and not runmode.unattended_state().enabled:
        return "声明为无人值守下单,但无人值守开关当前是关闭状态"
    return None


class _MemoryTransaction:
    """Record dry-run/rejection facts for compatibility; never authorizes broker writes."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def latest(self) -> dict[str, Any] | None:
        return self._events[-1] if self._events else None

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        event = {
            **dict(payload),
            "record_id": event_id,
            "event_id": event_id,
            "sequence": len(self._events) + 1,
        }
        self._events.append(event)
        return event


def _base_fact(
    report: GuardReport,
    auth: Authorization,
    metadata: ExecutionMetadata,
    snapshot: experiment.ExperimentSnapshot,
    policy: experiment.ExperimentPolicy,
    *,
    created_at: datetime,
    event_at: datetime,
) -> dict[str, Any]:
    proposed = report.proposed
    order = report.order
    object_id = metadata.object_id or (
        f"{proposed.market}_{proposed.symbol}" if proposed.market and proposed.symbol else proposed.symbol
    )
    return {
        "system_name": SYSTEM_NAME,
        "app_version": __version__,
        "strategy_id": metadata.strategy_id,
        "instruction_code": proposed.instruction_code,
        "object_id": object_id,
        "action": proposed.action,
        "market": proposed.market,
        "symbol": proposed.symbol,
        "name": proposed.name,
        "qty": order.qty if order is not None else 0,
        "limit_price": str(order.limit_price if order is not None else Decimal("0")),
        "notional": str(order.notional if order is not None else Decimal("0")),
        "proposed_order": _proposal_entry(proposed),
        "proposed_qty": _json_value(proposed.qty),
        "proposed_limit_price": _json_value(proposed.limit_price),
        "proposed_notional": str(order.notional) if order is not None else None,
        "model": metadata.model,
        "llm_provider": metadata.llm_provider,
        "confidence": metadata.confidence,
        "experiment_policy_result": "PENDING",
        "experiment_policy_reasons": [],
        "experiment_policy_config": policy.config.as_entry(),
        "experiment_snapshot": snapshot.as_entry(),
        "authorization_kind": auth.kind.value,
        "execution_state": ExecutionState.PREPARED.value,
        "broker_provider": None,
        "broker_receipt": None,
        "broker_order_reference": None,
        "outcome": "pending",
        "submitted_unknown": False,
        "created_at": created_at.isoformat(),
        "attempted_at": None,
        "completed_at": None,
        "event_at": event_at.isoformat(),
        "wtbh": proposed.wtbh,
        "message": "durable execution intent prepared",
        "reason": proposed.reason,
        "risk_note": proposed.risk_note,
        "规范化步骤": list(order.passed if order is not None else ()),
        "授权": _authorization_entry(auth),
    }


def _transition(
    prior: Mapping[str, Any],
    state: ExecutionState,
    *,
    at: datetime,
    message: str,
    policy_result: str | None = None,
    policy_reasons: Sequence[Mapping[str, str]] | None = None,
    broker_provider: str | None = None,
    broker_order_reference: str | None = None,
    attempted: bool = False,
    completed: bool = False,
    submitted_unknown: bool = False,
) -> dict[str, Any]:
    fact = dict(prior)
    fact.update({
        "execution_state": state.value,
        "outcome": _outcome_for_state(state).value,
        "event_at": at.isoformat(),
        "message": message,
        "submitted_unknown": submitted_unknown,
    })
    if policy_result is not None:
        fact["experiment_policy_result"] = policy_result
    if policy_reasons is not None:
        fact["experiment_policy_reasons"] = [dict(reason) for reason in policy_reasons]
    if broker_provider is not None:
        fact["broker_provider"] = broker_provider
    if attempted:
        fact["attempted_at"] = at.isoformat()
    if completed:
        fact["completed_at"] = at.isoformat()
    if broker_order_reference is not None:
        fact["broker_order_reference"] = broker_order_reference
        fact["wtbh"] = broker_order_reference
        fact["broker_receipt"] = {"order_reference": broker_order_reference}
    return fact


def _proposed_matches(fact: Mapping[str, Any], proposed: ProposedOrder) -> bool:
    stored = fact.get("proposed_order")
    return isinstance(stored, Mapping) and dict(stored) == _proposal_entry(proposed)


def _terminal_from_existing(tx: Any, latest: Mapping[str, Any], now: datetime) -> ExecutionRecord | None:
    try:
        state = ExecutionState(str(latest.get("execution_state") or ""))
    except ValueError:
        reconciled = tx.append(_transition(
            latest,
            ExecutionState.RECONCILE_REQUIRED,
            at=now,
            message="journal state 无法识别；禁止自动重试，需要人工 reconciliation",
            completed=True,
        ))
        return _record_from_fact(reconciled, fallback_now=now)
    if state is ExecutionState.EXECUTING:
        reconciled = tx.append(_transition(
            latest,
            ExecutionState.RECONCILE_REQUIRED,
            at=now,
            message="恢复时发现 broker write 可能已发生但没有明确 receipt；禁止自动重试",
            completed=True,
        ))
        return _record_from_fact(reconciled, fallback_now=now)
    if state in REPLAY_BLOCKING_STATES:
        return _record_from_fact(latest, fallback_now=now)
    return None


def _execute_locked(
    tx: Any,
    report: GuardReport,
    auth: Authorization,
    *,
    durable: bool,
    broker: BrokerAdapter | None,
    broker_provider: Callable[[], BrokerAdapter | None] | None,
    now: datetime,
    policy: experiment.ExperimentPolicy,
    snapshot: experiment.ExperimentSnapshot,
    metadata: ExecutionMetadata,
) -> ExecutionRecord:
    latest = tx.latest()
    if latest is not None:
        if not _proposed_matches(latest, report.proposed):
            reconciled = tx.append(_transition(
                latest,
                ExecutionState.RECONCILE_REQUIRED,
                at=now,
                message="同一 instruction_code 对应了不同 proposed order；禁止提交并要求 reconciliation",
                completed=True,
            ))
            return _record_from_fact(reconciled, fallback_now=now)
        terminal = _terminal_from_existing(tx, latest, now)
        if terminal is not None:
            return terminal

    created_at = _datetime(latest.get("created_at"), now) if latest is not None else now
    prepared = tx.append(_base_fact(
        report, auth, metadata, snapshot, policy, created_at=created_at, event_at=now
    ))

    if not report.ok or report.order is None:
        reasons = [
            {"code": failure.code, "message": failure.message}
            for failure in report.failures
        ]
        rejected = tx.append(_transition(
            prepared,
            ExecutionState.REJECTED,
            at=now,
            message="execution integrity reject: " + ";".join(r["code"] for r in reasons),
            policy_result=experiment.PolicyResult.REJECT.value,
            policy_reasons=reasons,
            completed=True,
        ))
        record = _record_from_fact(rejected, fallback_now=now)
        record_sink(record)
        return record

    order = report.order
    if order.action == "cancel" and not order.wtbh:
        rejected = tx.append(_transition(
            prepared,
            ExecutionState.REJECTED,
            at=now,
            message="撤单缺少 broker order reference",
            policy_result=experiment.PolicyResult.REJECT.value,
            policy_reasons=[{
                "code": "CANCEL_REFERENCE_REQUIRED",
                "message": "撤单必须包含 broker order reference。",
            }],
            completed=True,
        ))
        record = _record_from_fact(rejected, fallback_now=now)
        record_sink(record)
        return record

    decision = policy.evaluate(order, snapshot)
    decision_reasons = [reason.as_entry() for reason in decision.reasons]
    if not decision.passed:
        rejected = tx.append(_transition(
            prepared,
            ExecutionState.REJECTED,
            at=now,
            message="experiment policy rejected the exact proposed order",
            policy_result=decision.result.value,
            policy_reasons=decision_reasons,
            completed=True,
        ))
        record = _record_from_fact(rejected, fallback_now=now)
        record_sink(record)
        return record

    denial = _authorization_denial(auth)
    if denial:
        rejected = tx.append(_transition(
            prepared,
            ExecutionState.NOT_AUTHORIZED,
            at=now,
            message=denial,
            policy_result=decision.result.value,
            policy_reasons=decision_reasons,
            completed=True,
        ))
        record = _record_from_fact(rejected, fallback_now=now)
        record_sink(record)
        return record

    authorized = tx.append(_transition(
        prepared,
        ExecutionState.AUTHORIZED,
        at=now,
        message="experiment policy and authorization passed",
        policy_result=decision.result.value,
        policy_reasons=decision_reasons,
    ))

    if auth.kind is AuthorizationKind.SIMULATION or not runmode.live_trading_allowed():
        message = (
            "SIMULATION: broker adapter write was not called"
            if auth.kind is AuthorizationKind.SIMULATION
            else "M0 verification lock: broker adapter write was not called"
        )
        simulated = tx.append(_transition(
            authorized,
            ExecutionState.SIMULATED,
            at=now,
            message=message,
            completed=True,
        ))
        record = _record_from_fact(simulated, fallback_now=now)
        record_sink(record)
        return record

    if not durable:
        unavailable = tx.append(_transition(
            authorized,
            ExecutionState.BROKER_UNAVAILABLE,
            at=now,
            message="durable execution journal 未提供；拒绝调用 broker write",
            completed=True,
        ))
        record = _record_from_fact(unavailable, fallback_now=now)
        record_sink(record)
        return record

    resolved = broker
    if resolved is None and broker_provider is not None:
        try:
            resolved = broker_provider()
        except Exception as exc:  # noqa: BLE001 - never persist provider internals
            logger.error("取得 BrokerAdapter 失败,异常类型=%s", exc.__class__.__name__)
    if resolved is None:
        unavailable = tx.append(_transition(
            authorized,
            ExecutionState.BROKER_UNAVAILABLE,
            at=now,
            message="未提供可用的 BrokerAdapter",
            completed=True,
        ))
        record = _record_from_fact(unavailable, fallback_now=now)
        record_sink(record)
        return record

    provider = str(getattr(resolved, "provider", "") or "").strip().lower()
    if not provider or len(provider) > 64 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in provider
    ):
        unavailable = tx.append(_transition(
            authorized,
            ExecutionState.BROKER_UNAVAILABLE,
            at=now,
            message="BrokerAdapter 缺少合法的通用非机密 provider identity",
            completed=True,
        ))
        record = _record_from_fact(unavailable, fallback_now=now)
        record_sink(record)
        return record

    runmode.assert_live_trading_allowed(
        what=f"{order.action} {order.market}{order.symbol} x{order.qty}"
    )
    executing = tx.append(_transition(
        authorized,
        ExecutionState.EXECUTING,
        at=now,
        message="durable EXECUTING fact persisted before BrokerAdapter write",
        broker_provider=provider,
        attempted=True,
    ))

    try:
        if order.action == "cancel":
            assert order.wtbh is not None
            resolved.cancel_order(order.wtbh)
            order_reference = order.wtbh
        else:
            order_reference = str(resolved.place_order(order) or "").strip()
    except BrokerError as exc:
        if exc.submitted_unknown:
            unknown = tx.append(_transition(
                executing,
                ExecutionState.SUBMITTED_UNKNOWN,
                at=now,
                message="broker write result unknown; automatic retry is permanently forbidden",
                broker_provider=provider,
                completed=True,
                submitted_unknown=True,
            ))
            record = _record_from_fact(unknown, fallback_now=now)
            record_sink(record)
            return record
        rejected = tx.append(_transition(
            executing,
            ExecutionState.REJECTED,
            at=now,
            message="broker explicitly rejected or failed the request; same instruction will not replay",
            broker_provider=provider,
            completed=True,
        ))
        record = _record_from_fact(rejected, fallback_now=now)
        record_sink(record)
        return record
    except Exception as exc:  # noqa: BLE001 - state is uncertain after EXECUTING
        logger.error(
            "BrokerAdapter write 未分类异常:instruction=%s type=%s",
            order.instruction_code,
            exc.__class__.__name__,
        )
        reconciled = tx.append(_transition(
            executing,
            ExecutionState.RECONCILE_REQUIRED,
            at=now,
            message="BrokerAdapter write 后出现未分类异常；可能已触达 broker，禁止自动重试",
            broker_provider=provider,
            completed=True,
        ))
        record = _record_from_fact(reconciled, fallback_now=now)
        record_sink(record)
        return record

    if order.action != "cancel" and not order_reference:
        unknown = tx.append(_transition(
            executing,
            ExecutionState.SUBMITTED_UNKNOWN,
            at=now,
            message="BrokerAdapter returned no order reference after write; automatic retry is forbidden",
            broker_provider=provider,
            completed=True,
            submitted_unknown=True,
        ))
        record = _record_from_fact(unknown, fallback_now=now)
        record_sink(record)
        return record

    submitted = tx.append(_transition(
        executing,
        ExecutionState.SUBMITTED,
        at=now,
        message="BrokerAdapter accepted the request",
        broker_provider=provider,
        broker_order_reference=order_reference,
        completed=True,
    ))
    record = _record_from_fact(submitted, fallback_now=now)
    record_sink(record)
    return record


def execute(
    report: GuardReport,
    auth: Authorization,
    *,
    journal: archive.ExecutionJournal | None = None,
    broker: BrokerAdapter | None = None,
    broker_provider: Callable[[], BrokerAdapter | None] | None = None,
    now: datetime | None = None,
    policy: experiment.ExperimentPolicy | None = None,
    snapshot: experiment.ExperimentSnapshot | None = None,
    metadata: ExecutionMetadata | None = None,
) -> ExecutionRecord:
    """Execute one GuardReport through policy, journal, idempotency, and BrokerAdapter."""
    stamp = now or datetime.now()
    active_policy = policy or experiment.ExperimentPolicy()
    active_snapshot = snapshot or experiment.ExperimentSnapshot.initial(active_policy.config)
    active_metadata = metadata or ExecutionMetadata()
    if journal is None:
        return _execute_locked(
            _MemoryTransaction(), report, auth, durable=False, broker=broker,
            broker_provider=broker_provider, now=stamp, policy=active_policy,
            snapshot=active_snapshot, metadata=active_metadata,
        )
    with journal.transaction(report.proposed.instruction_code) as tx:
        return _execute_locked(
            tx, report, auth, durable=True, broker=broker,
            broker_provider=broker_provider, now=stamp, policy=active_policy,
            snapshot=active_snapshot, metadata=active_metadata,
        )


def reconcile_incomplete(
    journal: archive.ExecutionJournal, *, now: datetime | None = None
) -> tuple[ExecutionRecord, ...]:
    """On process recovery, seal every durable EXECUTING fact against automatic retry."""
    stamp = now or datetime.now()
    latest_by_code: dict[str, Mapping[str, Any]] = {}
    for event in journal.iter_events():
        code = str(event.get("instruction_code") or "")
        if code:
            latest_by_code[code] = event

    recovered: list[ExecutionRecord] = []
    for code, event in latest_by_code.items():
        if event.get("execution_state") != ExecutionState.EXECUTING.value:
            continue
        with journal.transaction(code) as tx:
            latest = tx.latest()
            if latest is None or latest.get("execution_state") != ExecutionState.EXECUTING.value:
                continue
            reconciled = tx.append(_transition(
                latest,
                ExecutionState.RECONCILE_REQUIRED,
                at=stamp,
                message="process recovery found an incomplete broker write; automatic retry forbidden",
                completed=True,
            ))
            record = _record_from_fact(reconciled, fallback_now=stamp)
            record_sink(record)
            recovered.append(record)
    return tuple(recovered)


def submit(
    order: ValidatedOrder,
    auth: Authorization,
    *,
    journal: archive.ExecutionJournal | None = None,
    broker: BrokerAdapter | None = None,
    broker_provider: Callable[[], BrokerAdapter | None] | None = None,
    now: datetime | None = None,
    policy: experiment.ExperimentPolicy | None = None,
    snapshot: experiment.ExperimentSnapshot | None = None,
    metadata: ExecutionMetadata | None = None,
) -> ExecutionRecord:
    """Compatibility wrapper for an already validated order; ``execute`` is the main path."""
    proposed = ProposedOrder(
        instruction_code=order.instruction_code,
        action=order.action,
        market=order.market,
        symbol=order.symbol,
        name=order.name,
        qty=order.qty,
        limit_price=order.limit_price,
        wtbh=order.wtbh,
        reason=order.reason,
        risk_note=order.risk_note,
    )
    return execute(
        GuardReport(proposed=proposed, order=order, failures=()),
        auth,
        journal=journal,
        broker=broker,
        broker_provider=broker_provider,
        now=now,
        policy=policy,
        snapshot=snapshot,
        metadata=metadata,
    )


@dataclass(frozen=True)
class BatchResult:
    records: tuple[ExecutionRecord, ...] = ()
    blocked: tuple[GuardReport, ...] = ()

    @property
    def submitted_count(self) -> int:
        return sum(1 for record in self.records if record.outcome is Outcome.SUBMITTED)

    @property
    def submitted_unknown_count(self) -> int:
        return sum(1 for record in self.records if record.submitted_unknown)


def submit_reports(
    reports: Sequence[GuardReport],
    auth: Authorization,
    *,
    journal: archive.ExecutionJournal | None = None,
    broker: BrokerAdapter | None = None,
    broker_provider: Callable[[], BrokerAdapter | None] | None = None,
    now: datetime | None = None,
    policy: experiment.ExperimentPolicy | None = None,
    snapshot: experiment.ExperimentSnapshot | None = None,
    snapshot_provider: Callable[[GuardReport], experiment.ExperimentSnapshot] | None = None,
    metadata_by_code: Mapping[str, ExecutionMetadata] | None = None,
) -> BatchResult:
    """Execute a batch through the same coordinator, preserving every proposed order fact."""
    active_policy = policy or experiment.ExperimentPolicy()
    projected = snapshot or experiment.ExperimentSnapshot.initial(active_policy.config)
    records: list[ExecutionRecord] = []
    blocked: list[GuardReport] = []
    for report in reports:
        if not report.ok:
            blocked.append(report)
        current = snapshot_provider(report) if snapshot_provider is not None else projected
        metadata = (metadata_by_code or {}).get(
            report.proposed.instruction_code, ExecutionMetadata()
        )
        record = execute(
            report,
            auth,
            journal=journal,
            broker=broker,
            broker_provider=broker_provider,
            now=now,
            policy=active_policy,
            snapshot=current,
            metadata=metadata,
        )
        records.append(record)
        if (
            snapshot_provider is None
            and report.order is not None
            and record.experiment_policy_result == experiment.PolicyResult.PASS.value
            and record.execution_state in {
                ExecutionState.SUBMITTED,
                ExecutionState.SUBMITTED_UNKNOWN,
                ExecutionState.RECONCILE_REQUIRED,
                ExecutionState.SIMULATED,
            }
        ):
            projected = projected.after_values(
                action=record.action,
                symbol=record.symbol,
                qty=record.qty,
                notional=record.notional,
            )
    return BatchResult(records=tuple(records), blocked=tuple(blocked))


__all__ = [
    "AuthorizationKind", "Authorization", "ExecutionMetadata", "ExecutionState", "Outcome",
    "ExecutionRecord", "BrokerAdapter", "REPLAY_BLOCKING_STATES", "record_entry", "execute",
    "submit", "reconcile_incomplete", "BatchResult", "submit_reports",
]
