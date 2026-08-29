"""M2 Experiment #1 preparation, hard-gated activation, and model probes.

This module never talks to a broker.  It prepares private runtime configuration, performs the
real market/model checks required by the Owner, and only then appends the one durable
``EXPERIMENT_STARTED`` ledger fact.  A missing or unstable model leaves the runtime unstarted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as monotonic_time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import (
    catalog as catalog_mod,
    collect,
    context,
    execution,
    llm,
    model,
    prompts,
    runmode,
    runner,
    scheduler,
    simulation,
    state,
)


EXPERIMENT_ID = "llm-trading-lab-exp1"
DISPLAY_NAME = "Experiment #1 — Zhixing Reproduction"
BASELINE_MAIN_SHA = "323e0b2dfddea0ce6008c231f88edfd7b08c647e"
INITIAL_NAV_CNY = Decimal("1000")
BENCHMARK_OBJECT_ID = "SH_510300"
BENCHMARK_SYMBOL = "510300"
MARKET_TIMEZONE = "Asia/Shanghai"
MODEL_PROVIDER = "DeepSeek"
MODEL_FAMILY = "DeepSeek V4"
MODEL_VARIANT = "Pro"
MODEL_PROTOCOL = "openai_chat"
MODEL_LATENCY_TARGET_SECONDS = 15 * 60
STORAGE_KIND_DOCKER_VOLUME = "docker_named_volume"

SCHEDULE_TEXT = ("09:35", "10:00", "11:15", "13:15", "14:00", "14:45")
SCHEDULE_CONFIG = scheduler.ScheduleConfig(
    times=tuple(time.fromisoformat(item) for item in SCHEDULE_TEXT),
    max_jitter_seconds=180,
    window_minutes=20,
    jitter_salt="zhixing",
)

EXACT_UNIVERSE = (
    catalog_mod.TradeObject(
        object_id="SH_510300", market="SH", symbol="510300", name="沪深300ETF",
        kind=catalog_mod.KIND_TRADABLE, asset_type="ETF", lot_size=100,
        turnover_mode=catalog_mod.TURNOVER_T1,
    ),
    catalog_mod.TradeObject(
        object_id="SZ_159915", market="SZ", symbol="159915", name="创业板ETF",
        kind=catalog_mod.KIND_TRADABLE, asset_type="ETF", lot_size=100,
        turnover_mode=catalog_mod.TURNOVER_T1,
    ),
    catalog_mod.TradeObject(
        object_id="SH_512880", market="SH", symbol="512880", name="证券ETF",
        kind=catalog_mod.KIND_TRADABLE, asset_type="ETF", lot_size=100,
        turnover_mode=catalog_mod.TURNOVER_T1,
    ),
)


class ActivationError(RuntimeError):
    def __init__(self, code: str, messages: Sequence[str]) -> None:
        self.code = code
        self.messages = tuple(str(item) for item in messages)
        super().__init__("; ".join(self.messages))


@dataclass(frozen=True)
class ModelProbe:
    model_echo: str | None
    full_round_wall_clock_seconds: float
    object_count: int

    def as_metadata(self, *, observed_at: datetime) -> dict[str, Any]:
        return {
            "observed_at": observed_at.isoformat(),
            "json_output_contract": "PASS",
            "model_echo": self.model_echo,
            "full_round_object_count": self.object_count,
            "full_round_wall_clock_seconds": round(
                self.full_round_wall_clock_seconds, 3
            ),
            "operational_target_seconds": MODEL_LATENCY_TARGET_SECONDS,
        }


def experiment_ledger(root: Path) -> simulation.ExperimentLedger:
    return simulation.ExperimentLedger(
        Path(root),
        initial_cash=INITIAL_NAV_CNY,
        benchmark=simulation.SimulationBenchmarkConfig(
            buy_and_hold_symbol=BENCHMARK_SYMBOL
        ),
        require_explicit_start=True,
        experiment_id=EXPERIMENT_ID,
    )


def exact_catalog() -> catalog_mod.Catalog:
    return catalog_mod.Catalog(EXACT_UNIVERSE)


def _object_entry(obj: catalog_mod.TradeObject) -> dict[str, Any]:
    return {
        "object_id": obj.object_id,
        "name": obj.name,
        "asset_type": obj.asset_type,
        "lot_size": obj.lot_size,
        "turnover_mode": obj.turnover_mode,
    }


def _catalog_signature(objects: Sequence[catalog_mod.TradeObject]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            obj.object_id, obj.market, obj.symbol, obj.name, obj.kind,
            obj.asset_type, obj.lot_size, obj.turnover_mode,
        )
        for obj in objects
    )


def _schedule_signature(config: scheduler.ScheduleConfig) -> tuple[Any, ...]:
    return (
        config.as_text(), config.max_jitter_seconds,
        config.window_minutes, config.jitter_salt,
    )


def prepare_runtime(store: state.Store) -> tuple[str, ...]:
    """Create only missing private catalog/schedule state; never overwrite drift."""
    changed: list[str] = []
    current = store.catalog()
    if current.objects:
        if _catalog_signature(current.objects) != _catalog_signature(EXACT_UNIVERSE):
            raise ActivationError(
                "UNIVERSE_MISMATCH",
                ["runtime tradable universe 不等于 Owner 固定的三只 ETF"],
            )
    else:
        store.save_catalog(EXACT_UNIVERSE)
        changed.append("catalog")

    if store.schedule_path.exists():
        if _schedule_signature(store.schedule()) != _schedule_signature(SCHEDULE_CONFIG):
            raise ActivationError(
                "SCHEDULE_MISMATCH",
                ["runtime schedule 不等于 09:35/10:00/11:15/13:15/14:00/14:45"],
            )
    else:
        store.save_schedule(SCHEDULE_CONFIG)
        changed.append("schedule")
    return tuple(changed)


def _model_configuration_problems(settings: state.ModelSettings) -> list[str]:
    missing = []
    if not settings.endpoint.strip():
        missing.append("model endpoint")
    if not settings.name.strip():
        missing.append("exact model identifier")
    if not settings.secret.strip():
        missing.append("model API key")
    if missing:
        return ["未配置 " + ", ".join(missing)]

    problems = []
    if settings.provider.strip().casefold() != MODEL_PROVIDER.casefold():
        problems.append(f"provider 必须是 {MODEL_PROVIDER}")
    if settings.protocol != MODEL_PROTOCOL:
        problems.append(f"protocol 必须是 {MODEL_PROTOCOL}")
    identifier = settings.name.strip().casefold().replace("_", "-")
    if "flash" in identifier or "v4" not in identifier or "pro" not in identifier:
        problems.append(
            "exact model identifier 必须明确指向 DeepSeek V4 Pro，不能是 Flash"
        )
    return problems


def _broker_configured(store: state.Store) -> bool:
    broker = store.broker()
    return bool(broker.remote_url.strip() or broker.account.strip() or broker.password)


def configuration_problems(
    store: state.Store,
    ledger: simulation.ExperimentLedger,
    *,
    now: datetime,
    storage_kind: str,
    require_started: bool,
) -> tuple[str, ...]:
    problems: list[str] = []
    if not runmode.VERIFICATION_LOCK:
        problems.append("VERIFICATION_LOCK 不是 True")
    if scheduler.clock_zone_problem(now) is not None:
        problems.append("runtime timezone 不是 Asia/Shanghai")
    if _catalog_signature(store.catalog().objects) != _catalog_signature(EXACT_UNIVERSE):
        problems.append("tradable universe 不是 exact three-ETF universe")
    if _schedule_signature(store.schedule()) != _schedule_signature(SCHEDULE_CONFIG):
        problems.append("six-slot schedule 与 M2 固定时点不一致")
    if _broker_configured(store):
        problems.append("runtime 中存在 broker URL/account/password，M2 禁止启动")
    if storage_kind != STORAGE_KIND_DOCKER_VOLUME:
        problems.append("archives/runtime 未证明位于 Docker named volume")

    repo_root = Path(__file__).resolve().parents[2]
    archive_root = ledger.root.resolve()
    if archive_root == repo_root or repo_root in archive_root.parents:
        problems.append("archive root 位于 repository 内，不是隔离 durable storage")

    summary = ledger.summary()
    if require_started:
        if summary["activation_state"] != "EXPERIMENT_STARTED":
            problems.append("缺少 EXPERIMENT_STARTED durable fact")
    elif summary["activation_state"] == "NOT_STARTED":
        if (
            summary["current_cash"] != "1000.00"
            or summary["nav"] != "1000.00"
            or summary["positions"]
            or summary["open_synthetic_orders"]
        ):
            problems.append("initial ExperimentLedger 不是 1000 cash / NAV / empty positions")
    elif summary["activation_state"] != "EXPERIMENT_STARTED":
        problems.append("existing ledger 不是 M2 explicit activation ledger")
    return tuple(problems)


def runtime_problems(
    store: state.Store,
    ledger: simulation.ExperimentLedger,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Cheap daemon checks; model endpoint and market probes run only during activation."""
    problems = list(configuration_problems(
        store, ledger, now=now, storage_kind=STORAGE_KIND_DOCKER_VOLUME,
        require_started=True,
    ))
    model_problems = _model_configuration_problems(store.model())
    problems.extend(model_problems)
    regime = ledger.summary().get("current_model_regime")
    settings = store.model()
    if isinstance(regime, Mapping):
        if (
            str(regime.get("provider") or "") != settings.provider
            or str(regime.get("exact_model_identifier") or "") != settings.name
            or str(regime.get("protocol") or "") != settings.protocol
        ):
            problems.append(
                "runtime model config 与 durable current_model_regime 不一致"
            )
    return tuple(problems)


def _check_model_echo(requested: str, echoes: Sequence[str]) -> str | None:
    observed = tuple(dict.fromkeys(item for item in echoes if item))
    mismatch = [item for item in observed if item != requested]
    if mismatch:
        raise ActivationError(
            "MODEL_ROUTE_MISMATCH",
            [f"endpoint model echo {item!r} != requested {requested!r}" for item in mismatch],
        )
    return observed[0] if observed else None


def _json_contract_smoke(
    caller: llm.ModelCaller, target: model.ModelTarget
) -> str | None:
    request = model.build_request(
        target,
        system_prompt="你是 Experiment #1 启动探针。只返回一个 JSON 对象。",
        user_text='只回复这个 json:{"contract":"ok"}',
    )
    try:
        reply = caller.call(target, request, object_id="activation-contract-probe")
        payload = json.loads(model.strip_fence(reply.text))
    except (llm.LlmError, model.ModelError, json.JSONDecodeError) as exc:
        raise ActivationError("MODEL_JSON_CONTRACT_BLOCKING", [str(exc)]) from exc
    if not isinstance(payload, Mapping) or payload.get("contract") != "ok":
        raise ActivationError(
            "MODEL_JSON_CONTRACT_BLOCKING",
            ["V4 Pro endpoint 未返回约定的 JSON object"],
        )
    return reply.model_echo or None


def _full_round_probe(
    *,
    store: state.Store,
    ledger: simulation.ExperimentLedger,
    caller: llm.ModelCaller,
    target: model.ModelTarget,
    now: datetime,
) -> ModelProbe:
    catalog = exact_catalog()
    collector = collect.MarketCollector(store=store)
    try:
        data = collector.collect(now=now, catalog=catalog)
    finally:
        collector.close()
    missing = sorted(set(obj.object_id for obj in EXACT_UNIVERSE) - set(data.per_object))
    unusable = sorted(
        obj.object_id
        for obj in EXACT_UNIVERSE
        if (
            data.objects.get(obj.symbol) is None
            or not data.objects[obj.symbol].quote_is_today
            or data.objects[obj.symbol].last_price <= 0
        )
    )
    if missing or unusable or data.problems:
        details = []
        if missing:
            details.append("缺少当日行情:" + ",".join(missing))
        if unusable:
            details.append("行情不可作为当时 reference:" + ",".join(unusable))
        details.extend(str(item.get("message") or item) for item in data.problems)
        raise ActivationError("MARKET_DATA_NOT_READY", details)

    probe_runner = runner.Runner(
        store=store,
        archive_root=ledger.root,
        caller=caller,
        target=target,
        source=collector,
        broker_provider=None,
        authorization_kind=execution.AuthorizationKind.SIMULATION,
        paper_engine=simulation.PaperExecutionEngine(ledger),
    )
    projected = probe_runner._project_synthetic_account(data, catalog=catalog, now=now)
    shared = context.build_shared(
        输出要求=prompts.OUTPUT_SPEC,
        读取范围=projected.读取范围,
        市场数据列表=projected.市场数据列表,
        账户交易流水表=projected.账户交易流水表,
    )
    prompts_for_round = context.build_round(
        shared, probe_runner._带历史(projected.per_object), generated_at=now
    )
    plan = context.dispatch_plan(prompts_for_round)
    ordered = ((plan.warmup,) if plan.warmup else ()) + plan.rest
    if len(ordered) != len(EXACT_UNIVERSE):
        raise ActivationError(
            "MODEL_PROBE_SCALE_MISMATCH",
            [f"期望 3 个正式规模 prompt，实际 {len(ordered)} 个"],
        )

    echoes: list[str] = []
    started = monotonic_time.monotonic()
    for prompt in ordered:
        try:
            reply = caller.call(
                target,
                model.build_request(
                    target,
                    system_prompt=prompts.SYSTEM_PROMPT,
                    user_text=prompt.text,
                ),
                object_id=prompt.object_id,
            )
        except (llm.LlmError, model.ModelError) as exc:
            raise ActivationError("MODEL_ENDPOINT_BLOCKING", [str(exc)]) from exc
        judgment, problems = model.parse_judgment(
            reply.text, expect_object_id=prompt.object_id
        )
        if judgment is None:
            raise ActivationError(
                "MODEL_JSON_CONTRACT_BLOCKING",
                [f"{prompt.object_id}: {item.code}: {item.message}" for item in problems],
            )
        if reply.model_echo:
            echoes.append(reply.model_echo)
    duration = monotonic_time.monotonic() - started
    echo = _check_model_echo(target.name, echoes)
    if duration > MODEL_LATENCY_TARGET_SECONDS:
        raise ActivationError(
            "MODEL_LATENCY_BLOCKING",
            [
                f"V4 Pro 三标的完整一轮耗时 {duration:.3f}s，"
                f"超过 {MODEL_LATENCY_TARGET_SECONDS}s operational target"
            ],
        )
    return ModelProbe(
        model_echo=echo,
        full_round_wall_clock_seconds=duration,
        object_count=len(ordered),
    )


def _metadata(
    *,
    settings: state.ModelSettings,
    probe: ModelProbe,
    started_at: datetime,
    ledger: simulation.ExperimentLedger,
    storage_kind: str,
) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "display_name": DISPLAY_NAME,
        "started_at": started_at.isoformat(),
        "main_sha": BASELINE_MAIN_SHA,
        "model": {
            "provider": MODEL_PROVIDER,
            "model_family": MODEL_FAMILY,
            "variant": MODEL_VARIANT,
            "protocol": MODEL_PROTOCOL,
            "exact_model_identifier": settings.name,
            "model_echo": probe.model_echo,
        },
        "model_preflight": probe.as_metadata(observed_at=started_at),
        "tradable_universe": [_object_entry(obj) for obj in EXACT_UNIVERSE],
        "schedule": {
            "timezone": MARKET_TIMEZONE,
            "slots": list(SCHEDULE_TEXT),
            "max_jitter_seconds": SCHEDULE_CONFIG.max_jitter_seconds,
            "window_minutes": SCHEDULE_CONFIG.window_minutes,
        },
        "initial_nav_cny": "1000.00",
        "fee_model": ledger.fee_model.config.as_entry(),
        "benchmark": {
            "cash_nav_cny": "1000.00",
            "buy_and_hold_object_id": BENCHMARK_OBJECT_ID,
        },
        "simulation_mode": True,
        "authorization_kind": execution.AuthorizationKind.SIMULATION.value,
        "broker_provider": None,
        "verification_lock": True,
        "repository_verification": "scripts/verify.sh PASS",
        "storage_kind": storage_kind,
    }


def activate(
    *,
    store: state.Store,
    ledger: simulation.ExperimentLedger,
    now: datetime,
    storage_kind: str,
) -> dict[str, Any]:
    existing = ledger.activation_metadata()
    if existing is not None:
        problems = runtime_problems(store, ledger, now=now)
        if problems:
            raise ActivationError("RUNTIME_PRECONDITION_BLOCKING", problems)
        return {
            "status": "EXPERIMENT_STARTED",
            "idempotent": True,
            "experiment_metadata": existing,
            "account": ledger.summary(),
        }

    problems = configuration_problems(
        store, ledger, now=now, storage_kind=storage_kind, require_started=False
    )
    if problems:
        raise ActivationError("RUNTIME_PRECONDITION_BLOCKING", problems)
    model_problems = _model_configuration_problems(store.model())
    if model_problems:
        missing = any(item.startswith("未配置 ") for item in model_problems)
        raise ActivationError(
            "READY_BUT_MODEL_UNCONFIGURED" if missing else "MODEL_CONFIGURATION_INVALID",
            model_problems,
        )

    settings = store.model()
    target = settings.to_target()
    caller = llm.HttpCaller(credential=llm.Credential(settings.secret))
    smoke_echo = _json_contract_smoke(caller, target)
    if smoke_echo:
        _check_model_echo(target.name, [smoke_echo])
    probe = _full_round_probe(
        store=store, ledger=ledger, caller=caller, target=target, now=now
    )
    started_at = datetime.now(ZoneInfo(MARKET_TIMEZONE))
    metadata = _metadata(
        settings=settings, probe=probe, started_at=started_at,
        ledger=ledger, storage_kind=storage_kind,
    )
    ledger.activate(at=started_at, metadata=metadata)
    return {
        "status": "EXPERIMENT_STARTED",
        "idempotent": False,
        "experiment_metadata": metadata,
        "account": ledger.summary(),
    }


def first_future_slot(now: datetime) -> dict[str, Any] | None:
    for offset in range(370):
        day = now.date() + timedelta(days=offset)
        plan = scheduler.plan_day(day, config=SCHEDULE_CONFIG)
        if not plan.trading_day:
            continue
        for slot in plan.slots:
            fire_at = slot.fire_at.replace(tzinfo=ZoneInfo(MARKET_TIMEZONE))
            if fire_at > now:
                return {
                    "planned": slot.planned.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "fire_at": fire_at.isoformat(),
                    "jitter_seconds": slot.jitter_seconds,
                }
    return None


def _public_status(
    *, store: state.Store, ledger: simulation.ExperimentLedger, now: datetime
) -> dict[str, Any]:
    settings = store.model()
    summary = ledger.summary()
    model_problems = _model_configuration_problems(settings)
    model_missing = any(item.startswith("未配置 ") for item in model_problems)
    return {
        "experiment_id": EXPERIMENT_ID,
        "display_name": DISPLAY_NAME,
        "activation_state": summary["activation_state"],
        "model": {
            "provider": settings.provider,
            "model_family": MODEL_FAMILY,
            "variant": MODEL_VARIANT,
            "protocol": settings.protocol,
            "exact_model_identifier": settings.name,
            "configured": not bool(model_problems),
            "configuration_state": (
                "READY"
                if not model_problems
                else "UNCONFIGURED"
                if model_missing
                else "INVALID"
            ),
        },
        "universe": [_object_entry(obj) for obj in store.catalog().objects],
        "schedule": list(store.schedule().as_text()),
        "next_slot": first_future_slot(now),
        "account": summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zhixing.activation")
    parser.add_argument("command", choices=("prepare", "status", "start"))
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument(
        "--storage-kind", default="",
        help="start 只接受由 host runtime check 证明的 docker_named_volume",
    )
    args = parser.parse_args(argv)

    try:
        store = state.Store(args.runtime_dir)
        ledger = experiment_ledger(Path(args.archive_root))
        now = datetime.now(ZoneInfo(MARKET_TIMEZONE))
        changed = prepare_runtime(store) if args.command in {"prepare", "start"} else ()
        if args.command == "start":
            result = activate(
                store=store, ledger=ledger, now=now, storage_kind=args.storage_kind
            )
        else:
            model_problems = _model_configuration_problems(store.model())
            model_missing = any(
                item.startswith("未配置 ") for item in model_problems
            )
            result = {
                "status": (
                    "EXPERIMENT_STARTED"
                    if ledger.is_started()
                    else "READY_BUT_MODEL_UNCONFIGURED"
                    if model_missing
                    else "BLOCKED"
                    if model_problems
                    else "READY_TO_START_EXPERIMENT"
                ),
                "changed": list(changed),
                **_public_status(store=store, ledger=ledger, now=now),
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ActivationError as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "code": exc.code,
            "reasons": list(exc.messages),
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3
    except (state.StateError, simulation.ExperimentLedgerError, OSError) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "code": "RUNTIME_STATE_BLOCKING",
            "reasons": [str(exc)],
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3


__all__ = [
    "EXPERIMENT_ID", "DISPLAY_NAME", "BASELINE_MAIN_SHA", "INITIAL_NAV_CNY",
    "BENCHMARK_OBJECT_ID", "MARKET_TIMEZONE", "MODEL_PROVIDER", "MODEL_FAMILY",
    "MODEL_VARIANT", "MODEL_PROTOCOL", "MODEL_LATENCY_TARGET_SECONDS",
    "STORAGE_KIND_DOCKER_VOLUME", "SCHEDULE_TEXT", "SCHEDULE_CONFIG",
    "EXACT_UNIVERSE", "ActivationError", "ModelProbe", "experiment_ledger",
    "exact_catalog", "prepare_runtime", "configuration_problems", "runtime_problems",
    "activate", "first_future_slot", "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
