"""Official XTP Pro adapter and read-only runtime bridge.

The adapter keeps XTP identifiers and asynchronous callback details out of the
execution coordinator.  Financial writes are never retried: once an SDK method
is invoked, any exception or timeout is treated as an uncertain submission and
must be resolved with the query methods in this module.

Credentials are loaded only by :func:`load_private_config` from a private file
outside the repository.  Neither configuration values nor raw account identity
fields are logged, archived, or returned by the broker-neutral read models.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import platform
import stat
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar, Mapping, Protocol, Sequence

from . import __version__
from .broker import BrokerError
from .execution import (
    BrokerAsset,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerTrade,
)
from .guards import ValidatedOrder


PRIVATE_CONFIG_ENV = "ZHIXING_XTP_PRO_CONFIG"
DEFAULT_PRIVATE_CONFIG = Path.home() / ".config" / "llm-trading-lab" / "xtp-pro-test.json"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_PRIVATE_RESPONSE_FIELDS = frozenset({"account_id", "branch_pbu"})

_ORDER_MARKET_TO_XTP = {"SZ": 1, "SH": 2}
_ORDER_MARKET_FROM_XTP = {1: "SZ", 2: "SH", 3: "BJ", 4: "HK"}
_QUOTE_MARKET_TO_XTP = {"SH": 1, "SZ": 2}
_SIDE_TO_XTP = {"buy": 1, "sell": 2}
_SIDE_FROM_XTP = {1: "buy", 2: "sell"}

# XTP_ORDER_STATUS_TYPE
_XTP_ORDER_INIT = 0
_XTP_ORDER_ALL_TRADED = 1
_XTP_ORDER_PART_TRADED_QUEUEING = 2
_XTP_ORDER_PART_TRADED_NOT_QUEUEING = 3
_XTP_ORDER_NO_TRADE_QUEUEING = 4
_XTP_ORDER_CANCELLED = 5
_XTP_ORDER_REJECTED = 6
_XTP_ORDER_UNKNOWN = 7

# XTP_ORDER_SUBMIT_STATUS_TYPE
_XTP_INSERT_SUBMITTED = 1
_XTP_INSERT_ACCEPTED = 2
_XTP_INSERT_REJECTED = 3
_XTP_CANCEL_SUBMITTED = 4
_XTP_CANCEL_REJECTED = 5
_XTP_CANCEL_ACCEPTED = 6


class XtpProError(RuntimeError):
    """Non-secret XTP integration failure."""


class XtpProConfigError(XtpProError):
    """Private runtime configuration is absent or invalid."""


class XtpProSdkError(XtpProError):
    """Official native SDK could not be loaded or used."""


class XtpProQueryError(XtpProError):
    """A read-only query failed or timed out."""


@dataclass(frozen=True, repr=False)
class XtpProPrivateConfig:
    """Secrets and connection data held only in private runtime memory."""

    sdk_library_dir: Path
    runtime_dir: Path
    quote_config_file: Path
    client_id: int
    software_key: str = field(repr=False)
    local_ip: str = field(repr=False)
    trader_host: str = field(repr=False)
    trader_port: int = field(repr=False)
    trader_username: str = field(repr=False)
    trader_password: str = field(repr=False)
    quote_host: str = field(repr=False)
    quote_port: int = field(repr=False)
    quote_username: str = field(repr=False)
    quote_password: str = field(repr=False)
    probe_market: str
    probe_symbol: str
    timeout_seconds: float = 10.0
    socket_type: int = 1
    allow_writes: bool = False
    environment: str = "test"


def private_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the private config path without opening it or exposing its contents."""

    configured = path or os.environ.get(PRIVATE_CONFIG_ENV) or DEFAULT_PRIVATE_CONFIG
    return Path(configured).expanduser().resolve()


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(_REPOSITORY_ROOT)
    except ValueError:
        return False
    return True


def _required_text(raw: Mapping[str, Any], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise XtpProConfigError(f"private config field {field_name!r} is required")
    return value.strip()


def _required_port(raw: Mapping[str, Any], field_name: str) -> int:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise XtpProConfigError(f"private config field {field_name!r} must be a TCP port")
    return value


def load_private_config(
    path: str | os.PathLike[str] | None = None,
) -> XtpProPrivateConfig:
    """Load the test-only XTP config without logging or returning raw JSON.

    The file and every SDK runtime output directory must remain outside Git.  On
    POSIX hosts the config must be owner-only (``0600`` or stricter).
    """

    resolved = private_config_path(path)
    if _inside_repository(resolved):
        raise XtpProConfigError("XTP private config must be outside the repository")
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except FileNotFoundError as exc:
        raise XtpProConfigError("XTP private config file is not present") from exc
    if mode & 0o077:
        raise XtpProConfigError("XTP private config must not be group/world accessible")

    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise XtpProConfigError("XTP private config cannot be read as JSON") from exc
    if not isinstance(parsed, Mapping):
        raise XtpProConfigError("XTP private config root must be an object")

    environment = str(parsed.get("environment") or "").strip().lower()
    if environment != "test":
        raise XtpProConfigError("only environment='test' is accepted in M2 phase one")
    if parsed.get("allow_writes") is not False:
        raise XtpProConfigError("allow_writes must be false in M2 phase one")

    trader = parsed.get("trader")
    quote = parsed.get("quote")
    probe = parsed.get("market_data_probe")
    if not isinstance(trader, Mapping):
        raise XtpProConfigError("private config field 'trader' must be an object")
    if not isinstance(quote, Mapping):
        raise XtpProConfigError("private config field 'quote' must be an object")
    if not isinstance(probe, Mapping):
        raise XtpProConfigError("private config field 'market_data_probe' must be an object")

    client_id = parsed.get("client_id")
    if isinstance(client_id, bool) or not isinstance(client_id, int) or not (1 <= client_id <= 24):
        raise XtpProConfigError("client_id must be an integer in the normal-user range 1..24")
    timeout = parsed.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= 120):
        raise XtpProConfigError("timeout_seconds must be between 1 and 120")

    sdk_library_dir = Path(_required_text(parsed, "sdk_library_dir")).expanduser().resolve()
    runtime_dir = Path(_required_text(parsed, "runtime_dir")).expanduser().resolve()
    quote_config_file = Path(_required_text(quote, "config_file")).expanduser().resolve()
    if any(
        _inside_repository(private_path)
        for private_path in (sdk_library_dir, runtime_dir, quote_config_file)
    ):
        raise XtpProConfigError("XTP SDK, runtime, and quote config paths must be outside Git")
    try:
        quote_config_mode = stat.S_IMODE(quote_config_file.stat().st_mode)
    except FileNotFoundError as exc:
        raise XtpProConfigError("XTP quote config file is not present") from exc
    if quote_config_mode & 0o077:
        raise XtpProConfigError("XTP quote config must not be group/world accessible")

    probe_market = _required_text(probe, "market").upper()
    if probe_market not in _QUOTE_MARKET_TO_XTP:
        raise XtpProConfigError("market_data_probe.market must be SH or SZ")

    socket_type = parsed.get("socket_type", 1)
    if socket_type != 1:
        raise XtpProConfigError("M2 phase one uses the official TCP socket_type=1 only")

    return XtpProPrivateConfig(
        sdk_library_dir=sdk_library_dir,
        runtime_dir=runtime_dir,
        quote_config_file=quote_config_file,
        client_id=client_id,
        software_key=_required_text(parsed, "software_key"),
        local_ip=_required_text(parsed, "local_ip"),
        trader_host=_required_text(trader, "host"),
        trader_port=_required_port(trader, "port"),
        trader_username=_required_text(trader, "username"),
        trader_password=_required_text(trader, "password"),
        quote_host=_required_text(quote, "host"),
        quote_port=_required_port(quote, "port"),
        quote_username=_required_text(quote, "username"),
        quote_password=_required_text(quote, "password"),
        probe_market=probe_market,
        probe_symbol=_required_text(probe, "symbol"),
        timeout_seconds=float(timeout),
        socket_type=socket_type,
        allow_writes=False,
        environment=environment,
    )


class XtpProTransport(Protocol):
    """Narrow synchronous surface used by :class:`XtpProBroker`."""

    sdk_version: str

    def insert_order(self, payload: Mapping[str, Any]) -> int: ...

    def cancel_order(self, order_xtp_id: int) -> int: ...

    def query_exact_order(self, order_xtp_id: int) -> Mapping[str, Any] | None: ...

    def query_orders(
        self, *, symbol: str, begin: datetime | None, end: datetime | None
    ) -> Sequence[Mapping[str, Any]]: ...

    def query_trades(
        self,
        *,
        order_xtp_id: int | None,
        symbol: str,
        begin: datetime | None,
        end: datetime | None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def query_asset(self) -> Mapping[str, Any]: ...

    def query_positions(self, *, symbol: str, market: str) -> Sequence[Mapping[str, Any]]: ...

    def market_data_probe(self, *, market: str, symbol: str) -> bool: ...


def stable_client_order_id(instruction_code: str) -> int:
    """Map the durable instruction id to XTP's unsigned 32-bit client reference.

    The primary durable relation remains ``instruction_code -> order_xtp_id`` in
    the execution journal.  This deterministic secondary reference permits
    query-only recovery when the SDK call may have reached XTP before the local
    process received ``order_xtp_id``.  A hash collision is treated as ambiguous
    reconciliation, never as permission to submit again.
    """

    normalized = str(instruction_code or "").strip()
    if not normalized:
        raise ValueError("instruction_code is required")
    digest = hashlib.sha256(b"zhixing/xtp_pro/order_client_id/v1\0" + normalized.encode()).digest()
    value = int.from_bytes(digest[:4], "big")
    return value or 1


def map_xtp_order_status(
    order_status: object,
    submit_status: object,
    *,
    quantity: object = 0,
    filled_quantity: object = 0,
) -> tuple[BrokerOrderStatus, bool]:
    """Map official XTP order/submit states to execution semantics."""

    raw_order = _integer(order_status)
    raw_submit = _integer(submit_status)
    total = max(0, _integer(quantity))
    filled = max(0, _integer(filled_quantity))

    if raw_order == _XTP_ORDER_REJECTED or raw_submit == _XTP_INSERT_REJECTED:
        return BrokerOrderStatus.REJECTED, True
    if raw_order == _XTP_ORDER_ALL_TRADED or (total > 0 and filled >= total):
        return BrokerOrderStatus.FILLED, True
    if raw_order == _XTP_ORDER_CANCELLED:
        return BrokerOrderStatus.CANCELLED, True
    if raw_order == _XTP_ORDER_PART_TRADED_NOT_QUEUEING:
        return BrokerOrderStatus.PARTIALLY_FILLED, True
    if raw_order == _XTP_ORDER_PART_TRADED_QUEUEING or (0 < filled < total):
        return BrokerOrderStatus.PARTIALLY_FILLED, False
    if raw_order == _XTP_ORDER_NO_TRADE_QUEUEING:
        return BrokerOrderStatus.ACCEPTED_SUBMITTED, False
    if raw_order == _XTP_ORDER_INIT and raw_submit in {
        _XTP_INSERT_SUBMITTED,
        _XTP_INSERT_ACCEPTED,
        _XTP_CANCEL_SUBMITTED,
        _XTP_CANCEL_REJECTED,
    }:
        return BrokerOrderStatus.ACCEPTED_SUBMITTED, False
    if raw_order == _XTP_ORDER_INIT and raw_submit == _XTP_CANCEL_ACCEPTED:
        return BrokerOrderStatus.RECONCILE_REQUIRED, False
    if raw_order == _XTP_ORDER_UNKNOWN:
        return BrokerOrderStatus.RECONCILE_REQUIRED, False
    return BrokerOrderStatus.RECONCILE_REQUIRED, False


def _integer(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


def _reference(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized and normalized != "0" else None


def _map_order(row: Mapping[str, Any]) -> BrokerOrder:
    quantity = max(0, _integer(row.get("quantity")))
    filled = max(0, _integer(row.get("qty_traded")))
    remaining = max(0, _integer(row.get("qty_left")))
    status, terminal = map_xtp_order_status(
        row.get("order_status"),
        row.get("order_submit_status"),
        quantity=quantity,
        filled_quantity=filled,
    )
    return BrokerOrder(
        provider="xtp_pro",
        order_reference=str(row.get("order_xtp_id") or "").strip(),
        status=status,
        market=_ORDER_MARKET_FROM_XTP.get(_integer(row.get("market")), "UNKNOWN"),
        symbol=str(row.get("ticker") or "").strip(),
        side=_SIDE_FROM_XTP.get(_integer(row.get("side")), "unknown"),
        quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=remaining,
        limit_price=_decimal(row.get("price")),
        client_order_reference=_reference(row.get("order_client_id")),
        broker_local_reference=_reference(row.get("order_local_id")),
        exchange_order_reference=_reference(row.get("order_exch_id")),
        cancel_order_reference=_reference(row.get("order_cancel_xtp_id")),
        provider_status=str(_integer(row.get("order_status"))),
        provider_submit_status=str(_integer(row.get("order_submit_status"))),
        terminal=terminal,
    )


def _map_trade(row: Mapping[str, Any]) -> BrokerTrade:
    market_code = _integer(row.get("market"))
    exec_id = _reference(row.get("exec_id"))
    report_index = _reference(row.get("report_index"))
    trade_reference = exec_id or (
        f"{market_code}:{report_index}" if report_index is not None else ""
    )
    return BrokerTrade(
        provider="xtp_pro",
        trade_reference=trade_reference,
        order_reference=str(row.get("order_xtp_id") or "").strip(),
        market=_ORDER_MARKET_FROM_XTP.get(market_code, "UNKNOWN"),
        symbol=str(row.get("ticker") or "").strip(),
        side=_SIDE_FROM_XTP.get(_integer(row.get("side")), "unknown"),
        quantity=max(0, _integer(row.get("quantity"))),
        price=_decimal(row.get("price")),
        amount=_decimal(row.get("trade_amount")),
        client_order_reference=_reference(row.get("order_client_id")),
        broker_local_reference=_reference(row.get("order_local_id")),
        exchange_order_reference=_reference(row.get("order_exch_id")),
        traded_at=_reference(row.get("trade_time")),
    )


@dataclass(frozen=True)
class InstructionReconciliation:
    """Query-only result for one durable instruction; never authorizes a write."""

    instruction_code: str
    client_order_reference: str
    status: BrokerOrderStatus
    candidate_count: int
    order: BrokerOrder | None
    trades: tuple[BrokerTrade, ...]


@dataclass
class XtpProBroker:
    """Broker-neutral adapter over the official XTP Pro SDK transport."""

    transport: XtpProTransport
    writes_enabled: bool = False

    provider: ClassVar[str] = "xtp_pro"

    def place_order(self, order: ValidatedOrder) -> str:
        """Invoke ``insertOrder`` exactly once; never retry an uncertain result."""

        if not self.writes_enabled:
            raise BrokerError("XTP Pro writes are disabled for the read-only integration phase")
        market = _ORDER_MARKET_TO_XTP.get(str(order.market or "").upper())
        side = _SIDE_TO_XTP.get(str(order.action or "").lower())
        if market is None or side is None:
            raise BrokerError("XTP Pro supports only SH/SZ stock BUY/SELL in this adapter")
        payload = {
            "ticker": order.symbol,
            "market": market,
            "price": float(order.limit_price),
            "quantity": order.qty,
            "price_type": 1,
            "side": side,
            "position_effect": 0,
            "business_type": 0,
            "order_client_id": stable_client_order_id(order.instruction_code),
        }
        try:
            order_xtp_id = int(self.transport.insert_order(payload))
        except Exception as exc:  # result is uncertain after the single SDK invocation
            raise BrokerError(
                "XTP Pro insertOrder result is unknown; query/reconcile only and do not resubmit",
                submitted_unknown=True,
            ) from exc
        if order_xtp_id <= 0:
            raise BrokerError("XTP Pro explicitly reported that insertOrder was not sent")
        return str(order_xtp_id)

    def cancel_order(self, wtbh: str) -> None:
        """Invoke ``cancelOrder`` exactly once; never retry an uncertain result."""

        if not self.writes_enabled:
            raise BrokerError("XTP Pro writes are disabled for the read-only integration phase")
        order_xtp_id = _positive_order_reference(wtbh)
        try:
            cancel_xtp_id = int(self.transport.cancel_order(order_xtp_id))
        except Exception as exc:  # result is uncertain after the single SDK invocation
            raise BrokerError(
                "XTP Pro cancelOrder result is unknown; query/reconcile only and do not repeat",
                submitted_unknown=True,
            ) from exc
        if cancel_xtp_id <= 0:
            raise BrokerError("XTP Pro explicitly reported that cancelOrder was not sent")

    def query_exact_order(self, order_reference: str) -> BrokerOrder | None:
        row = self.transport.query_exact_order(_positive_order_reference(order_reference))
        return _map_order(row) if row is not None else None

    def query_orders(
        self,
        *,
        symbol: str = "",
        begin: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[BrokerOrder]:
        return tuple(
            _map_order(row)
            for row in self.transport.query_orders(symbol=symbol, begin=begin, end=end)
        )

    def query_trades(
        self,
        *,
        order_reference: str | None = None,
        symbol: str = "",
        begin: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[BrokerTrade]:
        order_xtp_id = (
            _positive_order_reference(order_reference) if order_reference is not None else None
        )
        return tuple(
            _map_trade(row)
            for row in self.transport.query_trades(
                order_xtp_id=order_xtp_id,
                symbol=symbol,
                begin=begin,
                end=end,
            )
        )

    def query_asset(self) -> BrokerAsset:
        row = self.transport.query_asset()
        return BrokerAsset(
            provider=self.provider,
            total_asset=_decimal(row.get("total_asset")),
            buying_power=_decimal(row.get("buying_power")),
            security_asset=_decimal(row.get("security_asset")),
            cash_balance=_decimal(row.get("banlance")),
            withholding_amount=_decimal(row.get("withholding_amount")),
        )

    def query_positions(
        self, *, symbol: str = "", market: str = ""
    ) -> Sequence[BrokerPosition]:
        return tuple(
            BrokerPosition(
                provider=self.provider,
                market=_ORDER_MARKET_FROM_XTP.get(_integer(row.get("market")), "UNKNOWN"),
                symbol=str(row.get("ticker") or "").strip(),
                name=str(row.get("ticker_name") or "").strip(),
                total_quantity=max(0, _integer(row.get("total_qty"))),
                sellable_quantity=max(0, _integer(row.get("sellable_qty"))),
                average_price=_decimal(row.get("avg_price")),
                market_value=_decimal(row.get("market_value")),
                unrealized_pnl=_decimal(row.get("unrealized_pnl")),
            )
            for row in self.transport.query_positions(symbol=symbol, market=market)
        )

    def market_data_connectivity(self, *, market: str, symbol: str) -> bool:
        """Require a successful subscription and an actual depth-market-data callback."""

        normalized_market = str(market or "").upper()
        if normalized_market not in _QUOTE_MARKET_TO_XTP or not str(symbol or "").strip():
            raise XtpProQueryError("market-data probe requires SH/SZ and a symbol")
        return bool(
            self.transport.market_data_probe(market=normalized_market, symbol=str(symbol).strip())
        )

    def reconcile_instruction(
        self, instruction_code: str, expected_order: ValidatedOrder
    ) -> InstructionReconciliation:
        """Resolve an uncertain submission using orders/trades only.

        No candidate or more than one exact candidate is deliberately ambiguous.
        The caller must keep ``SUBMITTED_UNKNOWN``/``RECONCILE_REQUIRED`` and must
        not call :meth:`place_order` again.
        """

        client_id = stable_client_order_id(instruction_code)
        candidates = [
            order
            for order in self.query_orders(symbol=expected_order.symbol)
            if order.client_order_reference == str(client_id)
            and order.market == str(expected_order.market).upper()
            and order.symbol == expected_order.symbol
            and order.side == expected_order.action
            and order.quantity == expected_order.qty
            and order.limit_price == expected_order.limit_price
        ]
        if len(candidates) != 1:
            return InstructionReconciliation(
                instruction_code=instruction_code,
                client_order_reference=str(client_id),
                status=BrokerOrderStatus.RECONCILE_REQUIRED,
                candidate_count=len(candidates),
                order=None,
                trades=(),
            )
        order = candidates[0]
        trades = tuple(self.query_trades(order_reference=order.order_reference))
        return InstructionReconciliation(
            instruction_code=instruction_code,
            client_order_reference=str(client_id),
            status=order.status,
            candidate_count=1,
            order=order,
            trades=trades,
        )


def _positive_order_reference(value: object) -> int:
    normalized = str(value or "").strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        raise BrokerError("XTP Pro order reference must be a positive order_xtp_id")
    return int(normalized)


@dataclass
class _PendingQuery:
    event: threading.Event = field(default_factory=threading.Event)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error_code: int = 0


def _clean_sdk_row(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): item
        for key, item in value.items()
        if str(key) not in _PRIVATE_RESPONSE_FIELDS
    }


def _sdk_error_code(value: object) -> int:
    return _integer(value.get("error_id")) if isinstance(value, Mapping) else 0


def _xtp_time(value: datetime | None) -> int:
    if value is None:
        return 0
    local = value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value
    return int(local.strftime("%Y%m%d%H%M%S") + f"{local.microsecond // 1000:03d}")


class OfficialXtpProTransport:
    """In-process bridge for official bindings built for the backend interpreter.

    The vendor's prebuilt Python 3.9 package is exercised by the separate
    ``tests.xtp_pro_test_environment`` smoke because the project backend itself
    requires Python 3.11 or newer.
    """

    def __init__(
        self, config: XtpProPrivateConfig, *, connect_market_data: bool = False
    ) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[tuple[str, int], _PendingQuery] = {}
        self._unknown_orders: set[int] = set()
        self._quote_events: dict[tuple[str, str], threading.Event] = {}
        self._trader_session = 0
        self._quote_logged_in = False
        self._trader_module, self._quote_module = self._load_modules(config.sdk_library_dir)
        self._trader = self._make_trader_bridge()
        self._quote = self._make_quote_bridge()
        try:
            self._connect_trader()
            if connect_market_data:
                self.connect_market_data()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _load_modules(library_dir: Path) -> tuple[Any, Any]:
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            raise XtpProSdkError("official XTP Pro native SDK requires Linux x86_64")
        required = (
            "libxtpxtraderapi.so",
            "libxtpxquoteapi.so",
            "vnxtpxtrader.so",
            "vnxtpxquote.so",
        )
        if not library_dir.is_dir() or any(not (library_dir / name).is_file() for name in required):
            raise XtpProSdkError("official XTP Pro SDK library directory is incomplete")
        try:
            ctypes.CDLL(str(library_dir / "libxtpxtraderapi.so"), mode=ctypes.RTLD_GLOBAL)
            ctypes.CDLL(str(library_dir / "libxtpxquoteapi.so"), mode=ctypes.RTLD_GLOBAL)
            sys.path.insert(0, str(library_dir))
            trader_module = importlib.import_module("vnxtpxtrader")
            quote_module = importlib.import_module("vnxtpxquote")
        except (ImportError, OSError) as exc:
            raise XtpProSdkError("official XTP Pro native modules could not be loaded") from exc
        return trader_module, quote_module

    def _make_trader_bridge(self) -> Any:
        owner = self
        base = self._trader_module.TraderApi

        class TraderBridge(base):
            def __init__(self) -> None:
                super().__init__()

            def onDisconnected(self, session_id: int, reason: int) -> None:
                owner._trader_session = 0

            def onUnknownOrder(self, order_xtp_id: int, session_id: int) -> None:
                owner._unknown_orders.add(int(order_xtp_id))

            def onQueryOrder(
                self, data: object, error: object, reqid: int, last: object, session_id: int
            ) -> None:
                owner._query_callback("orders", data, error, reqid, last)

            def onQueryTrade(
                self, data: object, error: object, reqid: int, last: object, session_id: int
            ) -> None:
                owner._query_callback("trades", data, error, reqid, last)

            def onQueryPosition(
                self, data: object, error: object, reqid: int, last: object, session_id: int
            ) -> None:
                owner._query_callback("positions", data, error, reqid, last)

            def onQueryAsset(
                self, data: object, error: object, reqid: int, last: object, session_id: int
            ) -> None:
                owner._query_callback("asset", data, error, reqid, last)

        return TraderBridge()

    def _make_quote_bridge(self) -> Any:
        owner = self
        base = self._quote_module.QuoteApi

        class QuoteBridge(base):
            def __init__(self) -> None:
                super().__init__()

            def onDisconnected(self, reason: int) -> None:
                owner._quote_logged_in = False

            def onDepthMarketData(
                self,
                data: object,
                bid1_qty_list: object,
                bid1_counts: object,
                max_bid1_count: object,
                ask1_qty_list: object,
                ask1_count: object,
                max_ask1_count: object,
            ) -> None:
                if not isinstance(data, Mapping):
                    return
                symbol = str(data.get("ticker") or "").strip()
                exchange = _integer(data.get("exchange_id"))
                market = {1: "SH", 2: "SZ"}.get(exchange, "")
                event = owner._quote_events.get((market, symbol))
                if event is not None:
                    event.set()

        return QuoteBridge()

    def _private_runtime_directories(self) -> tuple[Path, Path]:
        runtime = self._config.runtime_dir
        trader_dir = runtime / "trader"
        quote_dir = runtime / "quote"
        for directory in (runtime, trader_dir, quote_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        return trader_dir, quote_dir

    def _connect_trader(self) -> None:
        trader_dir, _ = self._private_runtime_directories()
        # Fatal-only native SDK logs remain in the private runtime directory.
        self._trader.createTraderApi(self._config.client_id, str(trader_dir), 0)
        self._trader.subscribePublicTopic(2)
        self._trader.setSoftwareKey(self._config.software_key)
        self._trader.setSoftwareVersion(f"zhixing-{__version__}"[:15])
        self._trader.setHeartBeatInterval(15)
        session = int(
            self._trader.login(
                self._config.trader_host,
                self._config.trader_port,
                self._config.trader_username,
                self._config.trader_password,
                self._config.socket_type,
                self._config.local_ip,
            )
        )
        if session <= 0:
            raise XtpProSdkError(
                f"XTP Pro trader login failed (error_code={self._last_error_code(self._trader)})"
            )
        self._trader_session = session
        self.sdk_version = str(self._trader.getApiVersion() or "unknown")

    def connect_market_data(self) -> None:
        """Establish the quote session separately from the trader query session."""

        if self._quote_logged_in:
            return
        _, quote_dir = self._private_runtime_directories()
        self._quote.createQuoteApi(self._config.client_id, str(quote_dir), 0, False)
        self._quote.setHeartBeatInterval(15)
        if self._quote.setConfigFile(str(self._config.quote_config_file)) is not True:
            raise XtpProSdkError(
                f"XTP Pro quote config was rejected (error_code={self._last_error_code(self._quote)})"
            )
        quote_result = int(
            self._quote.login(
                self._config.quote_host,
                self._config.quote_port,
                self._config.quote_username,
                self._config.quote_password,
                self._config.socket_type,
                self._config.local_ip,
            )
        )
        if quote_result != 0:
            raise XtpProSdkError(
                f"XTP Pro quote login failed (error_code={self._last_error_code(self._quote)})"
            )
        self._quote_logged_in = True

    @staticmethod
    def _last_error_code(api: Any) -> int:
        try:
            return _sdk_error_code(api.getApiLastError())
        except Exception:
            return -1

    def _next_query(self, kind: str) -> tuple[int, _PendingQuery]:
        with self._lock:
            self._request_id += 1
            pending = _PendingQuery()
            self._pending[(kind, self._request_id)] = pending
            return self._request_id, pending

    def _query_callback(
        self, kind: str, data: object, error: object, reqid: int, last: object
    ) -> None:
        pending = self._pending.get((kind, int(reqid)))
        if pending is None:
            return
        row = _clean_sdk_row(data)
        if row:
            pending.rows.append(row)
        code = _sdk_error_code(error)
        if code:
            pending.error_code = code
        if bool(last):
            pending.event.set()

    def _wait_query(
        self, kind: str, reqid: int, pending: _PendingQuery, request_result: object
    ) -> tuple[Mapping[str, Any], ...]:
        if _integer(request_result) != 0:
            with self._lock:
                self._pending.pop((kind, reqid), None)
            raise XtpProQueryError(
                f"XTP Pro {kind} query was not accepted (error_code={self._last_error_code(self._trader)})"
            )
        completed = pending.event.wait(self._config.timeout_seconds)
        with self._lock:
            self._pending.pop((kind, reqid), None)
        if not completed:
            raise XtpProQueryError(f"XTP Pro {kind} query timed out")
        if pending.error_code:
            raise XtpProQueryError(
                f"XTP Pro {kind} query failed (error_code={pending.error_code})"
            )
        return tuple(pending.rows)

    def insert_order(self, payload: Mapping[str, Any]) -> int:
        return int(self._trader.insertOrder(dict(payload), self._trader_session))

    def cancel_order(self, order_xtp_id: int) -> int:
        return int(self._trader.cancelOrder(order_xtp_id, self._trader_session))

    def query_exact_order(self, order_xtp_id: int) -> Mapping[str, Any] | None:
        reqid, pending = self._next_query("orders")
        rows = self._wait_query(
            "orders",
            reqid,
            pending,
            self._trader.queryOrderByXTPID(order_xtp_id, self._trader_session, reqid),
        )
        if len(rows) > 1:
            raise XtpProQueryError("XTP Pro exact-order query returned multiple rows")
        if order_xtp_id in self._unknown_orders:
            row = dict(rows[0]) if rows else {"order_xtp_id": order_xtp_id}
            row["order_status"] = _XTP_ORDER_UNKNOWN
            return row
        return rows[0] if rows else None

    def query_orders(
        self, *, symbol: str, begin: datetime | None, end: datetime | None
    ) -> Sequence[Mapping[str, Any]]:
        reqid, pending = self._next_query("orders")
        request = {"ticker": str(symbol or "").strip(), "begin_time": _xtp_time(begin), "end_time": _xtp_time(end)}
        rows = self._wait_query(
            "orders",
            reqid,
            pending,
            self._trader.queryOrders(request, self._trader_session, reqid),
        )
        return tuple(
            ({**row, "order_status": _XTP_ORDER_UNKNOWN}
             if _integer(row.get("order_xtp_id")) in self._unknown_orders else row)
            for row in rows
        )

    def query_trades(
        self,
        *,
        order_xtp_id: int | None,
        symbol: str,
        begin: datetime | None,
        end: datetime | None,
    ) -> Sequence[Mapping[str, Any]]:
        reqid, pending = self._next_query("trades")
        if order_xtp_id is not None:
            result = self._trader.queryTradesByXTPID(
                order_xtp_id, self._trader_session, reqid
            )
        else:
            request = {
                "ticker": str(symbol or "").strip(),
                "begin_time": _xtp_time(begin),
                "end_time": _xtp_time(end),
            }
            result = self._trader.queryTrades(request, self._trader_session, reqid)
        return self._wait_query("trades", reqid, pending, result)

    def query_asset(self) -> Mapping[str, Any]:
        reqid, pending = self._next_query("asset")
        rows = self._wait_query(
            "asset",
            reqid,
            pending,
            self._trader.queryAsset(self._trader_session, reqid),
        )
        if len(rows) != 1:
            raise XtpProQueryError("XTP Pro asset query did not return exactly one row")
        return rows[0]

    def query_positions(self, *, symbol: str, market: str) -> Sequence[Mapping[str, Any]]:
        normalized_market = str(market or "").upper()
        if symbol and normalized_market not in _ORDER_MARKET_TO_XTP:
            raise XtpProQueryError("an exact XTP position query requires SH or SZ market")
        reqid, pending = self._next_query("positions")
        result = self._trader.queryPosition(
            str(symbol or "").strip(),
            self._trader_session,
            reqid,
            _ORDER_MARKET_TO_XTP.get(normalized_market, 0),
        )
        return self._wait_query("positions", reqid, pending, result)

    def market_data_probe(self, *, market: str, symbol: str) -> bool:
        self.connect_market_data()
        key = (market, symbol)
        event = threading.Event()
        self._quote_events[key] = event
        exchange = _QUOTE_MARKET_TO_XTP[market]
        ticker_list = [{"ticker": symbol}]
        try:
            result = int(self._quote.subscribeMarketData(ticker_list, 1, exchange))
            if result != 0:
                raise XtpProQueryError(
                    f"XTP Pro market-data subscription failed (error_code={self._last_error_code(self._quote)})"
                )
            return event.wait(self._config.timeout_seconds)
        finally:
            self._quote_events.pop(key, None)
            try:
                self._quote.unSubscribeMarketData(ticker_list, 1, exchange)
            except Exception:
                pass

    def close(self) -> None:
        """Close SDK sessions without exposing identifiers or suppressing prior results."""

        try:
            if self._quote_logged_in:
                self._quote.logout()
        except Exception:
            pass
        try:
            if self._trader_session:
                self._trader.logout(self._trader_session)
        except Exception:
            pass


def validate_official_sdk_environment(config: XtpProPrivateConfig) -> bool:
    """Load both native modules without logging in or opening a network connection."""

    OfficialXtpProTransport._load_modules(config.sdk_library_dir)
    return True


__all__ = [
    "PRIVATE_CONFIG_ENV",
    "DEFAULT_PRIVATE_CONFIG",
    "XtpProError",
    "XtpProConfigError",
    "XtpProSdkError",
    "XtpProQueryError",
    "XtpProPrivateConfig",
    "XtpProTransport",
    "XtpProBroker",
    "InstructionReconciliation",
    "OfficialXtpProTransport",
    "validate_official_sdk_environment",
    "private_config_path",
    "load_private_config",
    "stable_client_order_id",
    "map_xtp_order_status",
]
