"""CCXT-based exchange adapter - supports 100+ exchanges via CCXT library.

This adapter makes any CCXT-supported exchange work with the ExchangeInterface,
enabling seamless switching between Hyperliquid, Binance, Kraken, etc.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

try:
    import ccxt
except ImportError:
    ccxt = None

from forven.exchange.interface import (
    ExchangeInterface,
    Order,
    OrderResult,
    Position,
)

log = logging.getLogger("forven.exchange.ccxt")


class CCXTExchange(ExchangeInterface):
    """
    CCXT-based exchange adapter implementing ExchangeInterface.

    Supports 100+ exchanges: Binance, Kraken, Coinbase, Kucoin, Bybit, OKX, etc.

    Usage:
        ```python
        # Initialize with Binance
        exchange = CCXTExchange(exchange_id='binance', api_key='...', api_secret='...')

        # Or with testnet
        exchange = CCXTExchange(
            exchange_id='binance',
            api_key='...',
            api_secret='...',
            testnet=True
        )

        # Use like any other exchange
        positions = await exchange.get_positions()
        result = await exchange.market_order('BTC/USDT', 'buy', 1.0)
        ```
    """

    def __init__(
        self,
        exchange_id: str,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: bool = False,
        **exchange_kwargs: Any,
    ):
        """
        Initialize CCXT exchange adapter.

        Args:
            exchange_id: CCXT exchange ID (e.g., 'binance', 'kraken', 'coinbase')
            api_key: API key for the exchange
            api_secret: API secret for the exchange
            testnet: Use testnet/sandbox if available
            **exchange_kwargs: Additional arguments to pass to CCXT exchange constructor
        """
        if ccxt is None:
            raise ImportError(
                "ccxt library is required. Install it with: pip install ccxt"
            )

        self.exchange_id = exchange_id.lower()
        self.testnet = testnet

        # Create CCXT exchange instance
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
        except AttributeError:
            raise ValueError(
                f"Exchange '{self.exchange_id}' not found in CCXT. "
                f"Available exchanges: {', '.join(ccxt.exchanges[:10])}..."
            )

        # Initialize with credentials
        exchange_config = {
            "enableRateLimit": True,
            "apiKey": api_key or "",
            "secret": api_secret or "",
            **exchange_kwargs,
        }

        self.ccxt_exchange = exchange_class(exchange_config)

        # Enable testnet/demo mode.
        # Binance USDT-M futures testnet was deprecated in CCXT — use enable_demo_trading()
        # instead. All other exchanges use set_sandbox_mode().
        if testnet:
            try:
                if hasattr(self.ccxt_exchange, 'enable_demo_trading'):
                    self.ccxt_exchange.enable_demo_trading(True)
                    log.info("Demo trading mode enabled for %s", self.exchange_id)
                else:
                    self.ccxt_exchange.set_sandbox_mode(True)
                    log.info("Sandbox/testnet mode enabled for %s", self.exchange_id)
            except Exception as e:
                log.warning("Could not enable testnet/demo mode for %s: %s", self.exchange_id, e)

    # ======================== Account & Positions ========================

    async def get_account_value(self) -> float:
        """Get total account value in USD."""
        try:
            balance = await self._fetch_balance()
            # CCXT balance format: {'free': {...}, 'used': {...}, 'total': {...}}
            usd_balance = balance.get("total", {}).get("USD", 0.0)
            return float(usd_balance)
        except Exception as e:
            log.error("Failed to get account value: %s", e)
            return 0.0

    async def get_positions(self) -> List[Position]:
        """Get all open positions."""
        try:
            if not self.ccxt_exchange.has["fetchPositions"]:
                log.warning(
                    "Exchange %s does not support fetchPositions",
                    self.exchange_id,
                )
                return []

            positions_data = await self._fetch_positions()
            positions = []
            for pos in positions_data:
                if pos.get("contracts", 0) > 0 or pos.get("contractSize", 0) > 0:
                    positions.append(
                        Position(
                            symbol=pos.get("symbol", "").split("/")[0],
                            side="long"
                            if pos.get("side") == "long" or float(pos.get("contracts", 0)) > 0
                            else "short",
                            size=abs(float(pos.get("contracts", 0) or pos.get("info", {}).get("size", 0))),
                            entry_price=float(pos.get("entryPrice", 0) or 0),
                            current_price=float(pos.get("markPrice", 0) or 0),
                            leverage=int(pos.get("leverage", 1) or 1),
                            unrealized_pnl=float(
                                pos.get("unrealizedPnl", 0) or 0
                            ),
                        )
                    )
            return positions
        except Exception as e:
            log.error("Failed to get positions: %s", e)
            return []

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Get all open orders."""
        try:
            orders_data = await self._fetch_open_orders(symbol)
            orders = []
            for o in orders_data:
                orders.append(
                    Order(
                        order_id=o.get("id", ""),
                        symbol=o.get("symbol", "").split("/")[0],
                        side=o.get("side", ""),
                        price=float(o.get("price", 0) or 0),
                        size=float(o.get("amount", 0) or 0),
                        filled_size=float(o.get("filled", 0) or 0),
                        order_type=o.get("type", ""),
                        timestamp=float(o.get("timestamp", 0) or 0),
                        status=o.get("status", ""),
                    )
                )
            return orders
        except Exception as e:
            log.error("Failed to get open orders: %s", e)
            return []

    async def get_user_fills(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get recent fills."""
        try:
            trades = await self._fetch_my_trades(symbol, limit)
            return [
                {
                    "id": t.get("id"),
                    "symbol": t.get("symbol"),
                    "side": t.get("side"),
                    "price": float(t.get("price", 0)),
                    "amount": float(t.get("amount", 0)),
                    "timestamp": t.get("timestamp"),
                }
                for t in trades
            ]
        except Exception as e:
            log.error("Failed to get fills: %s", e)
            return []

    # ======================== Order Execution ========================

    async def market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        **kwargs: Any,
    ) -> OrderResult:
        """Execute a market order."""
        try:
            # Normalize side
            side_normalized = "buy" if side.lower() in ("buy", "long") else "sell"

            result = await self._create_market_order(symbol, side_normalized, size)
            if not result:
                return OrderResult(success=False, error="Market order failed")

            order_id = result.get("id")
            return OrderResult(
                success=True,
                order_id=order_id,
                raw_response=result,
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    async def limit_order(
        self, symbol: str, side: str, size: float, price: float, **kwargs: Any
    ) -> OrderResult:
        """Execute a limit order."""
        try:
            side_normalized = "buy" if side.lower() in ("buy", "long") else "sell"
            result = await self._create_limit_order(symbol, side_normalized, size, price)
            if not result:
                return OrderResult(success=False, error="Limit order failed")

            return OrderResult(
                success=True,
                order_id=result.get("id"),
                raw_response=result,
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    async def cancel_order(
        self, order_id: str, symbol: Optional[str] = None, **kwargs: Any
    ) -> bool:
        """Cancel an order."""
        try:
            await self._cancel_order(order_id, symbol)
            return True
        except Exception as e:
            log.error("Failed to cancel order: %s", e)
            return False

    async def close_position(self, symbol: str, **kwargs: Any) -> OrderResult:
        """Close a position with a market order."""
        try:
            positions = await self.get_positions()
            position = next((p for p in positions if p.symbol.upper() == symbol.upper()), None)
            if not position:
                return OrderResult(success=False, error=f"No position for {symbol}")

            side = "sell" if position.side == "long" else "buy"
            return await self.market_order(symbol, side, position.size)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    # ======================== Risk Orders ========================

    async def place_protective_stop(
        self, symbol: str, size: float, trigger_price: float, **kwargs: Any
    ) -> OrderResult:
        """Place a stop-loss order."""
        try:
            result = await self._create_stop_order(
                symbol, "sell", size, trigger_price, "stop"
            )
            return OrderResult(
                success=True if result else False,
                order_id=result.get("id") if result else None,
                raw_response=result,
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    async def place_take_profit(
        self, symbol: str, size: float, trigger_price: float, **kwargs: Any
    ) -> OrderResult:
        """Place a take-profit order."""
        try:
            result = await self._create_stop_order(
                symbol, "sell", size, trigger_price, "takeProfit"
            )
            return OrderResult(
                success=True if result else False,
                order_id=result.get("id") if result else None,
                raw_response=result,
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    # ======================== Leverage & Risk ========================

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        try:
            await self._set_leverage(symbol, leverage)
            return True
        except Exception as e:
            log.error("Failed to set leverage: %s", e)
            return False

    # ======================== Market Data ========================

    async def get_all_mids(self) -> Dict[str, float]:
        """Fetch current mid prices for all symbols."""
        try:
            tickers = await self._fetch_tickers()
            mids = {}
            for symbol, ticker in tickers.items():
                if "last" in ticker:
                    base = symbol.split("/")[0]
                    mids[base] = float(ticker["last"])
            return mids
        except Exception as e:
            log.error("Failed to get all mids: %s", e)
            return {}

    async def get_candles(
        self, symbol: str, interval: str = "1h", limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch OHLCV candles."""
        try:
            # Map interval to CCXT format if needed
            interval_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "1h": "1h",
                "4h": "4h",
                "1d": "1d",
            }
            timeframe = interval_map.get(interval, interval)

            ohlcv = await self._fetch_ohlcv(symbol, timeframe, limit)
            return [
                {
                    "timestamp": candle[0],
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
                for candle in ohlcv
            ]
        except Exception as e:
            log.error("Failed to get candles: %s", e)
            return []

    # ======================== Health & Metadata ========================

    async def health_check(self) -> bool:
        """Check if exchange is reachable."""
        try:
            await self.ccxt_exchange.fetch_status()
            return True
        except Exception:
            return False

    async def get_exchange_info(self) -> Dict[str, Any]:
        """Get exchange metadata."""
        return {
            "name": self.exchange_id,
            "type": "ccxt",
            "testnet": self.testnet,
            "symbols": list(self.ccxt_exchange.symbols[:10]) if self.ccxt_exchange.symbols else [],
        }

    # ======================== Internal CCXT Wrappers ========================

    async def _fetch_balance(self):
        """Wrapper for fetch_balance with error handling."""
        return await asyncio.to_thread(self.ccxt_exchange.fetch_balance)

    async def _fetch_positions(self):
        """Wrapper for fetch_positions."""
        return await asyncio.to_thread(self.ccxt_exchange.fetch_positions)

    async def _fetch_open_orders(self, symbol=None):
        """Wrapper for fetch_open_orders."""
        return await asyncio.to_thread(
            self.ccxt_exchange.fetch_open_orders, symbol
        )

    async def _fetch_my_trades(self, symbol=None, limit=100):
        """Wrapper for fetch_my_trades."""
        return await asyncio.to_thread(
            self.ccxt_exchange.fetch_my_trades, symbol, limit
        )

    async def _create_market_order(self, symbol, side, size):
        """Wrapper for create_market_order."""
        return await asyncio.to_thread(
            self.ccxt_exchange.create_market_order, symbol, side, size
        )

    async def _create_limit_order(self, symbol, side, size, price):
        """Wrapper for create_limit_order."""
        return await asyncio.to_thread(
            self.ccxt_exchange.create_limit_order, symbol, side, size, price
        )

    async def _cancel_order(self, order_id, symbol):
        """Wrapper for cancel_order."""
        return await asyncio.to_thread(
            self.ccxt_exchange.cancel_order, order_id, symbol
        )

    async def _create_stop_order(self, symbol, side, size, price, stop_type):
        """Wrapper for create_stop_order (if supported)."""
        if not hasattr(self.ccxt_exchange, "create_stop_order"):
            log.warning("Exchange %s does not support stop orders", self.exchange_id)
            return None
        return await asyncio.to_thread(
            self.ccxt_exchange.create_stop_order, symbol, side, size, price
        )

    async def _set_leverage(self, symbol, leverage):
        """Wrapper for set_leverage (if supported)."""
        if hasattr(self.ccxt_exchange, "set_leverage"):
            return await asyncio.to_thread(
                self.ccxt_exchange.set_leverage, leverage, symbol
            )
        log.warning("Exchange %s does not support set_leverage", self.exchange_id)

    async def _fetch_tickers(self):
        """Wrapper for fetch_tickers."""
        return await asyncio.to_thread(self.ccxt_exchange.fetch_tickers)

    async def _fetch_ohlcv(self, symbol, timeframe, limit):
        """Wrapper for fetch_ohlcv."""
        return await asyncio.to_thread(
            self.ccxt_exchange.fetch_ohlcv, symbol, timeframe, limit
        )
