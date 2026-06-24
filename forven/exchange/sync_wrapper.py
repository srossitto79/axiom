"""Synchronous wrappers around ExchangeInterface for use in sync contexts.

This module provides convenient sync wrappers when you're in a sync context
but need to call the async exchange interface. Use when you can't make your
function async (e.g., for CLI tools, lambda callbacks, or legacy sync code).

For new code and async contexts, prefer calling the async interface directly.
"""

import asyncio
from typing import Any, Dict, List, Optional

from forven.exchange.hyperliquid_adapter import HyperliquidExchange
from forven.exchange.interface import (
    ExchangeInterface,
    Order,
    OrderResult,
    Position,
)


def _get_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create an event loop for running async code in sync contexts."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

    return loop


class SyncExchange:
    """Synchronous wrapper around ExchangeInterface.

    Use this when you're in a sync context but need to call the exchange.
    For async code, use the interface directly instead.
    """

    def __init__(self, exchange: Optional[ExchangeInterface] = None, testnet: bool = False):
        self.exchange = exchange if exchange is not None else _build_exchange_for_settings(testnet)

    def get_account_value(self) -> float:
        """Get total account value in USD."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.get_account_value())

    def get_positions(self) -> List[Position]:
        """Get all open positions."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.get_positions())

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Get all open orders."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.get_open_orders(symbol))

    def get_user_fills(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get recent fills."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.get_user_fills(symbol, limit)
        )

    def market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        **kwargs: Any,
    ) -> OrderResult:
        """Execute a market order."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.market_order(
                symbol, side, size, stop_loss_price, take_profit_price, **kwargs
            )
        )

    def limit_order(
        self, symbol: str, side: str, size: float, price: float, **kwargs: Any
    ) -> OrderResult:
        """Execute a limit order."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.limit_order(symbol, side, size, price, **kwargs)
        )

    def cancel_order(
        self, order_id: str, symbol: Optional[str] = None, **kwargs: Any
    ) -> bool:
        """Cancel an order."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.cancel_order(order_id, symbol, **kwargs)
        )

    def close_position(self, symbol: str, **kwargs: Any) -> OrderResult:
        """Close a position."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.close_position(symbol, **kwargs)
        )

    def place_protective_stop(
        self, symbol: str, size: float, trigger_price: float, **kwargs: Any
    ) -> OrderResult:
        """Place a stop-loss order."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.place_protective_stop(symbol, size, trigger_price, **kwargs)
        )

    def place_take_profit(
        self, symbol: str, size: float, trigger_price: float, **kwargs: Any
    ) -> OrderResult:
        """Place a take-profit order."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.place_take_profit(symbol, size, trigger_price, **kwargs)
        )

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.set_leverage(symbol, leverage)
        )

    def get_all_mids(self) -> Dict[str, float]:
        """Get current mid prices."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.get_all_mids())

    def get_candles(
        self, symbol: str, interval: str = "1h", limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get OHLCV candles."""
        loop = _get_event_loop()
        return loop.run_until_complete(
            self.exchange.get_candles(symbol, interval, limit)
        )

    def health_check(self) -> bool:
        """Check if exchange is healthy."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.health_check())

    def get_exchange_info(self) -> Dict[str, Any]:
        """Get exchange metadata."""
        loop = _get_event_loop()
        return loop.run_until_complete(self.exchange.get_exchange_info())


# Module-level convenience function
_default_sync_exchange: Optional[SyncExchange] = None


def _build_exchange_for_settings(testnet: bool) -> ExchangeInterface:
    """Instantiate the correct ExchangeInterface based on the configured exchange setting."""
    try:
        from forven.api_core import _load_settings_payload, _load_settings_secrets
        s = _load_settings_payload()
        exchange_name = str(s.get("exchange") or "hyperliquid").strip().lower()
        secrets = _load_settings_secrets()
    except Exception:
        exchange_name = "hyperliquid"
        secrets = {}

    if exchange_name == "hyperliquid":
        return HyperliquidExchange(testnet=testnet)

    # All non-Hyperliquid exchanges go through CCXT.
    from forven.exchange.ccxt_adapter import CCXTExchange

    # Map Forven exchange names to CCXT exchange IDs.
    # binance → binanceusdm: spot (ccxt.binance) has no positions or perp contracts.
    # USDT-M Futures is the correct choice for Hyperliquid-equivalent perpetual trading.
    ccxt_id_map = {
        "binance": "binanceusdm",
        "kraken": "kraken",
        "okx": "okx",
        "coinbase": "coinbase",
        "generic_ccxt": str(secrets.get("generic_ccxt_exchange") or "binance").strip().lower(),
    }
    exchange_id = ccxt_id_map.get(exchange_name, exchange_name)

    # Pull credentials from the secrets store.
    key_prefix = exchange_name if exchange_name != "generic_ccxt" else "generic_ccxt"
    api_key = str(secrets.get(f"{key_prefix}_api_key") or "").strip() or None
    api_secret = str(secrets.get(f"{key_prefix}_api_secret") or "").strip() or None
    passphrase = str(secrets.get(f"{key_prefix}_api_passphrase") or "").strip() or None

    kwargs: dict = {}
    if passphrase:
        kwargs["password"] = passphrase

    use_testnet = bool(s.get(f"{key_prefix}_testnet", testnet))
    return CCXTExchange(
        exchange_id=exchange_id,
        api_key=api_key,
        api_secret=api_secret,
        testnet=use_testnet,
        **kwargs,
    )


def get_sync_exchange(testnet: bool = False) -> SyncExchange:
    """Get or create the default sync exchange wrapper, respecting the configured exchange."""
    global _default_sync_exchange
    if _default_sync_exchange is None:
        _default_sync_exchange = SyncExchange(_build_exchange_for_settings(testnet), testnet=testnet)
    return _default_sync_exchange


def reset_sync_exchange() -> None:
    """Force recreation of both exchange singletons — call after changing exchange settings."""
    global _default_sync_exchange
    _default_sync_exchange = None
    # Also reset the hyperliquid.py singleton so get_exchange() re-reads the setting.
    try:
        from forven.exchange import hyperliquid as _hl
        _hl._default_exchange = None
    except Exception:
        pass
