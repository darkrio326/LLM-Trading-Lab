# FTMO Lab M0 — Infrastructure-First Research Harness

## Objective

Build an infrastructure-first FTMO research harness that can develop, backtest, forward-test, and observe deterministic low-frequency strategies under strict FTMO-style risk controls.

M0 is research-only. It must remain simulation / Free Trial ready and must not enable paid Challenge purchase, real-money deposits, withdrawals, or real broker orders.

The design goal is to minimize Owner attention: Codex, CI, backtests, and monitoring may consume machine time, while the Owner is only asked to approve architecture changes, candidate-strategy promotion, and any transition beyond simulation.

## Architecture boundary

```text
Strategy module
    ↓ signal intent
Deterministic Risk Engine
    ↓ approved order intent
Execution / Simulation Adapter
    ↓ fills / account state
Journal + Metrics + Telemetry
    ↓
Cloudflare control plane / AI observer (later milestone)
```

The strategy layer must never own position sizing, drawdown policy, kill-switch behavior, credential handling, or execution authorization.

## M0 scope

### 1. Strategy/runtime separation

Add a thin strategy contract whose job is only to transform normalized market inputs into normalized signal intents.

The surrounding runtime owns:

- position sizing
- execution state
- realized and unrealized PnL
- daily/session boundaries
- drawdown accounting
- replay and idempotency
- kill switch / manual pause
- journal and telemetry emission

A strategy implementation must not bypass the risk engine.

### 2. Configurable FTMO-style risk engine

Vendor rules must be configuration rather than hard-coded assumptions. M0 begins with intentionally conservative internal defaults:

- max risk per trade: 0.25% of equity
- max aggregate open risk: 0.50% of equity
- daily loss soft stop: 1.00%
- total/equity drawdown soft stop: 4.00%
- stop opening new trades after 3 consecutive losing trades in the same trading day
- manual pause
- hard kill switch

Explicitly forbidden in M0:

- martingale
- grid recovery
- averaging down
- loss-recovery sizing
- risk escalation after losses
- strategy code mutating risk limits

Risk decisions must be reproducible from journaled state.

### 3. Strategy 001 — deliberately simple baseline

Implement one deterministic, low-complexity baseline strategy for validation, not optimization.

Preferred baseline shape:

- liquid FX instrument(s)
- low-frequency bar-based execution
- trend / breakout design
- small parameter surface, for example Donchian breakout + long-term EMA trend filter + ATR stop
- fixed declared parameter set for the first baseline run

Requirements:

- no LLM decision in the trade execution path
- no hidden discretionary input
- no parameter search before the baseline report exists
- every signal reproducible from historical inputs
- all strategy parameters declared in one config object/file

### 4. Validation pipeline

Provide a repeatable pipeline covering:

- historical backtest
- explicit in-sample / out-of-sample split
- walk-forward evaluation
- randomized / Monte Carlo trade-sequence stress where practical
- spread sensitivity
- commission sensitivity
- slippage sensitivity

Required report metrics:

- net return
- max drawdown
- max daily drawdown
- profit factor
- expectancy
- trade count
- win rate
- average win / average loss
- consecutive losses
- risk-limit near misses
- in-sample vs out-of-sample comparison

A smooth equity curve alone is not an acceptance criterion.

### 5. Forward-test / FTMO Free Trial readiness

M0 must define an adapter boundary for later MT5 / EA / bridge integration, but it must run without FTMO credentials and without sending orders to FTMO.

Normalize telemetry events for the later Cloudflare control plane:

- heartbeat
- account/equity snapshot
- signal generated
- signal rejected + reason
- position opened / closed
- daily loss usage
- total drawdown usage
- kill-switch event
- strategy version
- runtime version

M0 should define the schema/interface only. Production Cloudflare deployment is a later milestone.

## Safety boundary

M0 is restricted to simulation and research.

- No paid FTMO Challenge purchase.
- No broker deposit or withdrawal.
- No real broker order placement or cancellation.
- No credential values in Git, logs, reports, screenshots, or evidence bundles.
- No LLM may directly decide or execute a trade in M0.
- No strategy may bypass deterministic risk checks.
- Existing repository simulation safety guarantees must not be weakened.

## Acceptance criteria

1. Core risk engine has deterministic automated tests for:
   - per-trade risk
   - aggregate open risk
   - daily loss stop
   - total drawdown stop
   - consecutive-loss stop
   - pause
   - kill switch
2. Strategy 001 runs end-to-end through the same normalized risk/execution path used by tests.
3. Re-running the same historical data + configuration produces identical signals, fills under the same execution model, PnL, and risk decisions.
4. One documented command/workflow produces a report containing both in-sample and out-of-sample metrics.
5. Spread/slippage assumptions can be varied without editing strategy source.
6. Financial write capability remains disabled by default and M0 evidence can prove `financial_write_calls=0`.
7. Existing repository verification remains green.
8. No real-money capability is introduced as an incidental side effect.

## Promotion gate

Passing M0 does not authorize a paid FTMO Challenge.

The next milestone is a Free Trial / forward-test adapter. A paid entry should only be proposed after multiple independent forward-test windows remain inside the internal risk envelope and show positive out-of-sample expectancy.

## Owner attention budget

Design the work so machine time is cheap and human attention is scarce:

- Codex: implementation, tests, refactors, backtests, report generation
- CI / runner: repeatable validation
- later Cloudflare observer: heartbeat, telemetry, anomaly summaries
- later AI observer: review evidence and propose candidate changes; never execute trades directly
- Owner: architecture review, strategy promotion approval, explicit authorization for any paid or real-money transition
