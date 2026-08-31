"""Official XTP Pro 1.2.1 read-only test-environment smoke.

This file deliberately remains Python 3.9 compatible because the official
prebuilt Linux bindings target Python 3.9.13. It does not import ``zhixing``
and contains no insert/cancel call. Deterministic BrokerAdapter tests live in
``tests.xtp_pro_adapter`` and run under the normal project interpreter.

Only stage booleans, the public SDK version, and exception class names reach
stdout. Config values, endpoints, account snapshots, order/trade ids, session
ids, and SDK error messages never do.
"""

from __future__ import print_function

import ctypes
import importlib
import json
import os
import platform
import stat
import sys
import threading
from pathlib import Path


CONFIG_ENV = "ZHIXING_XTP_PRO_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config" / "llm-trading-lab" / "xtp-pro-test.json"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_FIELDS = frozenset(("account_id", "branch_pbu"))
QUOTE_EXCHANGE = {"SH": 1, "SZ": 2}


class SmokeError(RuntimeError):
    pass


class ConfigError(SmokeError):
    pass


class SdkError(SmokeError):
    pass


class QueryError(SmokeError):
    pass


def _inside_repository(path):
    try:
        path.resolve().relative_to(REPOSITORY_ROOT)
    except ValueError:
        return False
    return True


def _required_text(raw, name):
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("required private field is absent")
    return value.strip()


def _port(raw):
    value = raw.get("port")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("private port field is invalid")
    return value


def load_config():
    path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser().resolve()
    if _inside_repository(path):
        raise ConfigError("private config must remain outside repository")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise ConfigError("private config is absent")
    if mode & 0o077:
        raise ConfigError("private config permissions are too broad")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise ConfigError("private config is unreadable")
    if not isinstance(raw, dict):
        raise ConfigError("private config root is invalid")
    if str(raw.get("environment") or "").lower() != "test":
        raise ConfigError("only the test environment is accepted")
    if raw.get("allow_writes") is not False:
        raise ConfigError("allow_writes must be false")
    trader = raw.get("trader")
    quote = raw.get("quote")
    probe = raw.get("market_data_probe")
    if not isinstance(trader, dict) or not isinstance(quote, dict) or not isinstance(probe, dict):
        raise ConfigError("private nested config is invalid")

    client_id = raw.get("client_id")
    if isinstance(client_id, bool) or not isinstance(client_id, int) or not 1 <= client_id <= 24:
        raise ConfigError("client_id is invalid")
    timeout = raw.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ConfigError("timeout is invalid")
    if raw.get("socket_type", 1) != 1:
        raise ConfigError("only TCP socket type is accepted")

    sdk_library_dir = Path(_required_text(raw, "sdk_library_dir")).expanduser().resolve()
    runtime_dir = Path(_required_text(raw, "runtime_dir")).expanduser().resolve()
    quote_config = Path(_required_text(quote, "config_file")).expanduser().resolve()
    if any(_inside_repository(item) for item in (sdk_library_dir, runtime_dir, quote_config)):
        raise ConfigError("private runtime paths must remain outside repository")
    try:
        quote_config_mode = stat.S_IMODE(quote_config.stat().st_mode)
    except FileNotFoundError:
        raise ConfigError("private quote config is absent")
    if quote_config_mode & 0o077:
        raise ConfigError("private quote config permissions are too broad")
    market = _required_text(probe, "market").upper()
    if market not in QUOTE_EXCHANGE:
        raise ConfigError("probe market is invalid")

    # Keep the returned object private and never repr/print it.
    return {
        "sdk_library_dir": sdk_library_dir,
        "runtime_dir": runtime_dir,
        "quote_config": quote_config,
        "client_id": client_id,
        "software_key": _required_text(raw, "software_key"),
        "local_ip": _required_text(raw, "local_ip"),
        "socket_type": 1,
        "timeout": float(timeout),
        "trader": {
            "host": _required_text(trader, "host"),
            "port": _port(trader),
            "username": _required_text(trader, "username"),
            "password": _required_text(trader, "password"),
        },
        "quote": {
            "host": _required_text(quote, "host"),
            "port": _port(quote),
            "username": _required_text(quote, "username"),
            "password": _required_text(quote, "password"),
        },
        "probe_market": market,
        "probe_symbol": _required_text(probe, "symbol"),
    }


def load_sdk(library_dir):
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SdkError("official prebuilt SDK requires Linux x86_64")
    if sys.version_info[:2] != (3, 9):
        raise SdkError("official prebuilt SDK requires Python 3.9")
    required = (
        "libxtpxtraderapi.so",
        "libxtpxquoteapi.so",
        "vnxtpxtrader.so",
        "vnxtpxquote.so",
    )
    if not library_dir.is_dir() or any(not (library_dir / name).is_file() for name in required):
        raise SdkError("official SDK directory is incomplete")
    try:
        ctypes.CDLL(str(library_dir / "libxtpxtraderapi.so"), mode=ctypes.RTLD_GLOBAL)
        ctypes.CDLL(str(library_dir / "libxtpxquoteapi.so"), mode=ctypes.RTLD_GLOBAL)
        sys.path.insert(0, str(library_dir))
        trader_module = importlib.import_module("vnxtpxtrader")
        quote_module = importlib.import_module("vnxtpxquote")
    except (ImportError, OSError):
        raise SdkError("official native modules could not be loaded")
    return trader_module, quote_module


class Pending(object):
    def __init__(self):
        self.event = threading.Event()
        self.rows = []
        self.error = False


def _clean_row(value):
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items() if str(key) not in PRIVATE_FIELDS}


def _has_error(value):
    return isinstance(value, dict) and int(value.get("error_id") or 0) != 0


class ReadOnlySdk(object):
    def __init__(self, config, trader_module, quote_module):
        self.config = config
        self.trader_module = trader_module
        self.quote_module = quote_module
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.request_id = 0
        self.quote_events = {}
        self.session = 0
        self.quote_logged_in = False
        self.trader = self._trader_bridge()()
        self.quote = self._quote_bridge()()
        self.sdk_version = ""

    def _trader_bridge(self):
        owner = self
        base = self.trader_module.TraderApi

        class TraderBridge(base):
            def __init__(self):
                super(TraderBridge, self).__init__()

            def onDisconnected(self, session_id, reason):
                owner.session = 0

            def onQueryOrder(self, data, error, reqid, last, session_id):
                owner._callback("orders", data, error, reqid, last)

            def onQueryTrade(self, data, error, reqid, last, session_id):
                owner._callback("trades", data, error, reqid, last)

            def onQueryPosition(self, data, error, reqid, last, session_id):
                owner._callback("positions", data, error, reqid, last)

            def onQueryAsset(self, data, error, reqid, last, session_id):
                owner._callback("asset", data, error, reqid, last)

        return TraderBridge

    def _quote_bridge(self):
        owner = self
        base = self.quote_module.QuoteApi

        class QuoteBridge(base):
            def __init__(self):
                super(QuoteBridge, self).__init__()

            def onDisconnected(self, reason):
                owner.quote_logged_in = False

            def onDepthMarketData(
                self,
                data,
                bid1_qty_list,
                bid1_counts,
                max_bid1_count,
                ask1_qty_list,
                ask1_count,
                max_ask1_count,
            ):
                if not isinstance(data, dict):
                    return
                exchange = int(data.get("exchange_id") or 0)
                market = {1: "SH", 2: "SZ"}.get(exchange, "")
                symbol = str(data.get("ticker") or "").strip()
                event = owner.quote_events.get((market, symbol))
                if event is not None:
                    event.set()

        return QuoteBridge

    def connect_trader(self):
        runtime_dir = self.config["runtime_dir"]
        trader_dir = runtime_dir / "trader"
        quote_dir = runtime_dir / "quote"
        for directory in (runtime_dir, trader_dir, quote_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.trader.createTraderApi(self.config["client_id"], str(trader_dir), 0)
        self.trader.subscribePublicTopic(2)
        self.trader.setSoftwareKey(self.config["software_key"])
        self.trader.setSoftwareVersion("zhixing-m2-ro")
        self.trader.setHeartBeatInterval(15)
        endpoint = self.config["trader"]
        self.session = int(
            self.trader.login(
                endpoint["host"],
                endpoint["port"],
                endpoint["username"],
                endpoint["password"],
                self.config["socket_type"],
                self.config["local_ip"],
            )
        )
        if self.session <= 0:
            raise SdkError("trader login failed")
        self.sdk_version = str(self.trader.getApiVersion() or "")

    def connect_quote(self):
        if self.quote_logged_in:
            return
        quote_dir = self.config["runtime_dir"] / "quote"
        self.quote.createQuoteApi(self.config["client_id"], str(quote_dir), 0, False)
        self.quote.setHeartBeatInterval(15)
        if self.quote.setConfigFile(str(self.config["quote_config"])) is not True:
            raise SdkError("quote config failed")
        endpoint = self.config["quote"]
        result = int(
            self.quote.login(
                endpoint["host"],
                endpoint["port"],
                endpoint["username"],
                endpoint["password"],
                self.config["socket_type"],
                self.config["local_ip"],
            )
        )
        if result != 0:
            raise SdkError("quote login failed")
        self.quote_logged_in = True

    def _begin(self, kind):
        with self.pending_lock:
            self.request_id += 1
            pending = Pending()
            self.pending[(kind, self.request_id)] = pending
            return self.request_id, pending

    def _callback(self, kind, data, error, reqid, last):
        pending = self.pending.get((kind, int(reqid)))
        if pending is None:
            return
        row = _clean_row(data)
        if row:
            pending.rows.append(row)
        if _has_error(error):
            pending.error = True
        if bool(last):
            pending.event.set()

    def _finish(self, kind, reqid, pending, result):
        if int(result) != 0:
            with self.pending_lock:
                self.pending.pop((kind, reqid), None)
            raise QueryError("query request was not accepted")
        completed = pending.event.wait(self.config["timeout"])
        with self.pending_lock:
            self.pending.pop((kind, reqid), None)
        if not completed:
            raise QueryError("query timed out")
        if pending.error:
            raise QueryError("query callback reported an error")
        return tuple(pending.rows)

    def query_asset(self):
        reqid, pending = self._begin("asset")
        rows = self._finish(
            "asset", reqid, pending, self.trader.queryAsset(self.session, reqid)
        )
        if len(rows) != 1:
            raise QueryError("asset query row count is invalid")
        return rows

    def query_positions(self):
        reqid, pending = self._begin("positions")
        return self._finish(
            "positions",
            reqid,
            pending,
            self.trader.queryPosition("", self.session, reqid, 0),
        )

    def query_orders(self):
        reqid, pending = self._begin("orders")
        request = {"ticker": "", "begin_time": 0, "end_time": 0}
        return self._finish(
            "orders", reqid, pending, self.trader.queryOrders(request, self.session, reqid)
        )

    def query_exact_order(self, order_xtp_id):
        reqid, pending = self._begin("orders")
        return self._finish(
            "orders",
            reqid,
            pending,
            self.trader.queryOrderByXTPID(int(order_xtp_id), self.session, reqid),
        )

    def query_trades(self):
        reqid, pending = self._begin("trades")
        request = {"ticker": "", "begin_time": 0, "end_time": 0}
        return self._finish(
            "trades", reqid, pending, self.trader.queryTrades(request, self.session, reqid)
        )

    def market_data(self):
        self.connect_quote()
        market = self.config["probe_market"]
        symbol = self.config["probe_symbol"]
        event = threading.Event()
        self.quote_events[(market, symbol)] = event
        tickers = [{"ticker": symbol}]
        exchange = QUOTE_EXCHANGE[market]
        try:
            if int(self.quote.subscribeMarketData(tickers, 1, exchange)) != 0:
                raise QueryError("market subscription failed")
            if not event.wait(self.config["timeout"]):
                raise QueryError("depth market data timed out")
            return True
        finally:
            self.quote_events.pop((market, symbol), None)
            try:
                self.quote.unSubscribeMarketData(tickers, 1, exchange)
            except Exception:
                pass

    def close(self):
        try:
            if self.quote_logged_in:
                self.quote.logout()
        except Exception:
            pass
        try:
            if self.session:
                self.trader.logout(self.session)
        except Exception:
            pass


def stage(report, name, operation):
    try:
        value = operation()
    except Exception as exc:
        report[name] = "failed"
        report.setdefault("error_types", {})[name] = exc.__class__.__name__
        return None
    report[name] = "success"
    return value


def main():
    report = {
        "private_config": "not_run",
        "sdk_python_environment": "not_run",
        "test_login": "not_run",
        "asset_query": "not_run",
        "position_query": "not_run",
        "order_query": "not_run",
        "exact_order_query": "not_run",
        "trade_query": "not_run",
        "market_data": "not_run",
        "financial_write_calls": 0,
    }
    config = stage(report, "private_config", load_config)
    if config is None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    modules = stage(report, "sdk_python_environment", lambda: load_sdk(config["sdk_library_dir"]))
    if modules is None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    client = ReadOnlySdk(config, modules[0], modules[1])
    login_marker = stage(report, "test_login", client.connect_trader)
    if login_marker is None and report["test_login"] != "success":
        client.close()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    try:
        report["sdk_version"] = client.sdk_version
        stage(report, "asset_query", client.query_asset)
        stage(report, "position_query", client.query_positions)
        orders = stage(report, "order_query", client.query_orders)
        if orders:
            stage(
                report,
                "exact_order_query",
                lambda: client.query_exact_order(orders[0].get("order_xtp_id")),
            )
        else:
            report["exact_order_query"] = "not_applicable_no_orders"
        stage(report, "trade_query", client.query_trades)
        stage(report, "market_data", client.market_data)
    finally:
        client.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    required = (
        "sdk_python_environment",
        "test_login",
        "asset_query",
        "position_query",
        "order_query",
        "trade_query",
        "market_data",
    )
    return 0 if all(report.get(name) == "success" for name in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
