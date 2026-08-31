"""Deterministic XTP Pro adapter checks.

This module uses an in-memory transport only.  It never imports the native SDK,
loads private configuration, opens a network connection, or submits an order.
Run separately from the credential-gated test-environment smoke:

    PYTHONDONTWRITEBYTECODE=1 python -m tests.xtp_pro_adapter
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from zhixing import guards
from zhixing.broker import BrokerError
from zhixing.execution import BrokerOrderStatus
from zhixing.xtp_pro import XtpProBroker, map_xtp_order_status, stable_client_order_id


PASS, FAIL = "  [OK]", "  [!!]"
results: list[bool] = []


def check(label: str, condition: bool) -> None:
    results.append(condition)
    print(f"{PASS if condition else FAIL} {label}")


NOW = datetime(2026, 8, 31, 10, 0, 0)
CONTEXT = guards.ValidationContext(
    account=None,
    objects={
        "510300": guards.ObjectSnapshot(
            symbol="510300", last_price=Decimal("4.20"), is_etf=True
        )
    },
    now=NOW,
)


def validated_order(code: str = "xtp-fake-instruction") -> guards.ValidatedOrder:
    report = guards.validate(
        guards.ProposedOrder(
            code,
            "buy",
            "SH",
            "510300",
            "XTP Fake ETF",
            qty=100,
            limit_price="4.20",
        ),
        CONTEXT,
    )
    assert report.order is not None
    return report.order


def raw_order(
    *,
    instruction_code: str = "xtp-fake-instruction",
    order_xtp_id: int = 990001,
    order_status: int = 4,
    submit_status: int = 2,
    qty_traded: int = 0,
    qty_left: int = 100,
) -> dict[str, Any]:
    return {
        "order_xtp_id": order_xtp_id,
        "ticker": "510300",
        "market": 2,
        "order_client_id": stable_client_order_id(instruction_code),
        "order_local_id": "local-7",
        "order_status": order_status,
        "order_exch_id": "exchange-8",
        "side": 1,
        "order_submit_status": submit_status,
        "quantity": 100,
        "price": 4.2,
        "order_cancel_xtp_id": 0,
        "qty_traded": qty_traded,
        "qty_left": qty_left,
        # These identity fields must not be present in broker-neutral models.
        "account_id": "fake-account-must-not-escape",
        "branch_pbu": "fake-pbu-must-not-escape",
    }


class FakeTransport:
    sdk_version = "1.2.1-fake"

    def __init__(self) -> None:
        self.insert_calls = 0
        self.cancel_calls = 0
        self.query_calls = 0
        self.insert_result = 990001
        self.cancel_result = 990002
        self.insert_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.orders: list[Mapping[str, Any]] = [raw_order()]
        self.trades: list[Mapping[str, Any]] = [
            {
                "order_xtp_id": 990001,
                "ticker": "510300",
                "market": 2,
                "order_client_id": stable_client_order_id("xtp-fake-instruction"),
                "order_local_id": "local-7",
                "side": 1,
                "price": 4.2,
                "quantity": 40,
                "trade_amount": 168,
                "order_exch_id": "exchange-8",
                "report_index": 55,
                "exec_id": "exec-9",
                "trade_time": 20260831100102003,
            }
        ]
        self.last_insert_payload: Mapping[str, Any] | None = None

    def insert_order(self, payload: Mapping[str, Any]) -> int:
        self.insert_calls += 1
        self.last_insert_payload = dict(payload)
        if self.insert_error is not None:
            raise self.insert_error
        return self.insert_result

    def cancel_order(self, order_xtp_id: int) -> int:
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result

    def query_exact_order(self, order_xtp_id: int) -> Mapping[str, Any] | None:
        self.query_calls += 1
        return next(
            (row for row in self.orders if int(row.get("order_xtp_id") or 0) == order_xtp_id),
            None,
        )

    def query_orders(
        self, *, symbol: str, begin: datetime | None, end: datetime | None
    ) -> Sequence[Mapping[str, Any]]:
        self.query_calls += 1
        return tuple(row for row in self.orders if not symbol or row.get("ticker") == symbol)

    def query_trades(
        self,
        *,
        order_xtp_id: int | None,
        symbol: str,
        begin: datetime | None,
        end: datetime | None,
    ) -> Sequence[Mapping[str, Any]]:
        self.query_calls += 1
        return tuple(
            row
            for row in self.trades
            if (order_xtp_id is None or row.get("order_xtp_id") == order_xtp_id)
            and (not symbol or row.get("ticker") == symbol)
        )

    def query_asset(self) -> Mapping[str, Any]:
        self.query_calls += 1
        return {
            "total_asset": "1000.01",
            "buying_power": "600.01",
            "security_asset": "400",
            "banlance": "620.01",
            "withholding_amount": "20",
            "account_id": "fake-account-must-not-escape",
        }

    def query_positions(self, *, symbol: str, market: str) -> Sequence[Mapping[str, Any]]:
        self.query_calls += 1
        row = {
            "ticker": "510300",
            "ticker_name": "XTP Fake ETF",
            "market": 2,
            "total_qty": 100,
            "sellable_qty": 100,
            "avg_price": "4.10",
            "market_value": "420",
            "unrealized_pnl": "10",
            "account_id": "fake-account-must-not-escape",
        }
        return (row,) if not symbol or symbol == row["ticker"] else ()

    def market_data_probe(self, *, market: str, symbol: str) -> bool:
        self.query_calls += 1
        return market == "SH" and symbol == "510300"


print("\n=== XTP Pro status mapping ===")

expected_statuses = (
    ((0, 1), (BrokerOrderStatus.ACCEPTED_SUBMITTED, False)),
    ((4, 2), (BrokerOrderStatus.ACCEPTED_SUBMITTED, False)),
    ((0, 4), (BrokerOrderStatus.ACCEPTED_SUBMITTED, False)),
    ((0, 5), (BrokerOrderStatus.ACCEPTED_SUBMITTED, False)),
    ((0, 6), (BrokerOrderStatus.RECONCILE_REQUIRED, False)),
    ((6, 3), (BrokerOrderStatus.REJECTED, True)),
    ((2, 2), (BrokerOrderStatus.PARTIALLY_FILLED, False)),
    ((3, 2), (BrokerOrderStatus.PARTIALLY_FILLED, True)),
    ((1, 2), (BrokerOrderStatus.FILLED, True)),
    ((5, 6), (BrokerOrderStatus.CANCELLED, True)),
    ((7, 2), (BrokerOrderStatus.RECONCILE_REQUIRED, False)),
    ((99, 99), (BrokerOrderStatus.RECONCILE_REQUIRED, False)),
)
for raw, expected in expected_statuses:
    check(
        f"XTP order_status={raw[0]} submit_status={raw[1]} -> {expected[0].value}",
        map_xtp_order_status(*raw) == expected,
    )
check(
    "trade quantity can establish partially_filled when order callback omits that state",
    map_xtp_order_status(4, 2, quantity=100, filled_quantity=40)
    == (BrokerOrderStatus.PARTIALLY_FILLED, False),
)


print("\n=== Broker-neutral read mapping ===")

transport = FakeTransport()
broker = XtpProBroker(transport)
order = broker.query_exact_order("990001")
check(
    "provider and XTP primary/client/local/exchange identifiers are preserved",
    order is not None
    and order.provider == "xtp_pro"
    and order.order_reference == "990001"
    and order.client_order_reference == str(stable_client_order_id("xtp-fake-instruction"))
    and order.broker_local_reference == "local-7"
    and order.exchange_order_reference == "exchange-8",
)
check(
    "query exact order maps accepted/submitted without account identity",
    order is not None
    and order.status is BrokerOrderStatus.ACCEPTED_SUBMITTED
    and not hasattr(order, "account_id")
    and not hasattr(order, "branch_pbu"),
)
trade = broker.query_trades(order_reference="990001")[0]
check(
    "trade record links exec_id to order_xtp_id and order_client_id",
    trade.trade_reference == "exec-9"
    and trade.order_reference == "990001"
    and trade.client_order_reference == order.client_order_reference
    and trade.quantity == 40,
)
asset = broker.query_asset()
positions = broker.query_positions()
check(
    "asset query maps totals but excludes account identity",
    asset.total_asset == Decimal("1000.01") and not hasattr(asset, "account_id"),
)
check(
    "position query maps quantity/cost but excludes shareholder identity",
    len(positions) == 1
    and positions[0].market == "SH"
    and positions[0].sellable_quantity == 100
    and not hasattr(positions[0], "account_id"),
)
check(
    "market-data connectivity uses the read-only transport probe",
    broker.market_data_connectivity(market="SH", symbol="510300"),
)


print("\n=== At-most-once write semantics over FakeTransport ===")

expected = validated_order()
write_transport = FakeTransport()
write_broker = XtpProBroker(write_transport, writes_enabled=True)
reference = write_broker.place_order(expected)
check(
    "fake insert is invoked exactly once and returns order_xtp_id",
    reference == "990001" and write_transport.insert_calls == 1,
)
check(
    "instruction_code maps deterministically to XTP order_client_id",
    write_transport.last_insert_payload is not None
    and write_transport.last_insert_payload["order_client_id"]
    == stable_client_order_id(expected.instruction_code)
    and write_transport.last_insert_payload["market"] == 2,
)

disabled_transport = FakeTransport()
try:
    XtpProBroker(disabled_transport).place_order(expected)
except BrokerError as exc:
    check(
        "phase-one write gate rejects before any SDK transport call",
        disabled_transport.insert_calls == 0 and exc.submitted_unknown is False,
    )
else:
    check("phase-one write gate rejects before any SDK transport call", False)

timeout_transport = FakeTransport()
timeout_transport.insert_error = TimeoutError("fake timeout")
try:
    XtpProBroker(timeout_transport, writes_enabled=True).place_order(expected)
except BrokerError as exc:
    check(
        "SDK timeout after one fake insert becomes submitted_unknown and is not retried",
        timeout_transport.insert_calls == 1 and exc.submitted_unknown is True,
    )
else:
    check("SDK timeout after one fake insert becomes submitted_unknown and is not retried", False)

zero_transport = FakeTransport()
zero_transport.insert_result = 0
try:
    XtpProBroker(zero_transport, writes_enabled=True).place_order(expected)
except BrokerError as exc:
    check(
        "official zero result is an explicit single-attempt rejection",
        zero_transport.insert_calls == 1 and exc.submitted_unknown is False,
    )
else:
    check("official zero result is an explicit single-attempt rejection", False)

cancel_transport = FakeTransport()
cancel_transport.cancel_error = TimeoutError("fake timeout")
try:
    XtpProBroker(cancel_transport, writes_enabled=True).cancel_order("990001")
except BrokerError as exc:
    check(
        "SDK cancel timeout becomes submitted_unknown and cancel is not retried",
        cancel_transport.cancel_calls == 1 and exc.submitted_unknown is True,
    )
else:
    check("SDK cancel timeout becomes submitted_unknown and cancel is not retried", False)


print("\n=== instruction_code query-only reconciliation ===")

reconcile = broker.reconcile_instruction(expected.instruction_code, expected)
check(
    "instruction -> order_client_id -> order_xtp_id -> trade record is exact",
    reconcile.candidate_count == 1
    and reconcile.order is not None
    and reconcile.order.order_reference == "990001"
    and reconcile.trades[0].order_reference == reconcile.order.order_reference,
)
check(
    "reconciliation performs queries only and no fake writes",
    transport.insert_calls == 0 and transport.cancel_calls == 0,
)

ambiguous_transport = FakeTransport()
ambiguous_transport.orders.append(dict(ambiguous_transport.orders[0], order_xtp_id=990099))
ambiguous_broker = XtpProBroker(ambiguous_transport)
ambiguous = ambiguous_broker.reconcile_instruction(expected.instruction_code, expected)
check(
    "multiple matching XTP rows remain RECONCILE_REQUIRED and never resubmit",
    ambiguous.status is BrokerOrderStatus.RECONCILE_REQUIRED
    and ambiguous.candidate_count == 2
    and ambiguous.order is None
    and ambiguous_transport.insert_calls == 0,
)


print(f"\nXTP deterministic adapter: {sum(results)}/{len(results)} passed")
if not all(results):
    raise SystemExit(1)
