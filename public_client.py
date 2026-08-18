import asyncio
import json
import random
import threading
import time

import websocket


# ============================================================
# DERIV PUBLIC WEBSOCKET
# ============================================================
# One shared WebSocket is used for all symbols.
# This avoids opening 10 independent connections to the same
# public endpoint and makes reconnecting much more reliable.
# ============================================================

PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


class PublicMarketClient:

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.on_candle = None

        self._closed = False
        self._connected = False
        self._loop = None

        # symbol -> granularity
        self._subscriptions = {}

        # symbol -> currently forming candle
        self._current_candles = {}

        self._ws = None
        self._thread = None

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._connected = False

    # ========================================================
    # REQUEST WITH RETRIES
    # ========================================================

    def _request_sync(self, payload):
        last_error = None

        for attempt in range(4):
            ws = None
            try:
                ws = websocket.create_connection(
                    PUBLIC_WS_URL,
                    timeout=self.timeout,
                )

                ws.send(json.dumps(payload))

                while True:
                    raw = ws.recv()

                    if not raw:
                        continue

                    response = json.loads(raw)

                    if "error" in response:
                        error = response["error"]
                        raise RuntimeError(
                            f"Deriv API error: "
                            f"{error.get('code', 'UNKNOWN')} - "
                            f"{error.get('message', 'Unknown error')}"
                        )

                    return response

            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    delay = min(2 ** attempt, 8) + random.uniform(0, 0.8)
                    time.sleep(delay)

            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

        raise last_error

    async def _request(self, payload):
        return await asyncio.to_thread(self._request_sync, payload)

    # ========================================================
    # CANDLE HISTORY
    # ========================================================

    async def get_candle_history(self, symbol, granularity=60, count=200):
        response = await self._request(
            {
                "ticks_history": symbol,
                "end": "latest",
                "count": int(count),
                "style": "candles",
                "granularity": int(granularity),
            }
        )

        candles = response.get("candles", [])

        if not candles:
            raise RuntimeError(f"No candles received for {symbol}")

        return [
            {
                "epoch": int(c["epoch"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "granularity": int(granularity),
            }
            for c in candles
        ]

    # ========================================================
    # COMPATIBILITY
    # ========================================================

    async def get_candles(self, symbol, granularity=60, count=200):
        return await self.get_candle_history(symbol, granularity, count)

    async def get_ohlc(self, symbol, timeframe="1m", count=200):
        timeframe_map = {
            "1m": 60,
            "2m": 120,
            "5m": 300,
            "10m": 600,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }

        if timeframe not in timeframe_map:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        return await self.get_candle_history(
            symbol,
            timeframe_map[timeframe],
            count,
        )

    # ========================================================
    # ACTIVE SYMBOLS
    # ========================================================

    async def get_active_symbols(self):
        response = await self._request({"active_symbols": "brief"})
        return response.get("active_symbols", [])

    # ========================================================
    # CURRENT PRICE
    # ========================================================

    async def get_price(self, symbol):
        response = await self._request({"ticks": symbol})
        tick = response.get("tick")

        if not tick:
            raise RuntimeError(f"No tick received for {symbol}")

        return {
            "symbol": symbol,
            "quote": float(tick["quote"]),
            "epoch": int(tick["epoch"]),
        }

    # ========================================================
    # SEND SUBSCRIPTION
    # ========================================================

    def _send_subscription(self, symbol):
        ws = self._ws
        if ws is None:
            return False

        try:
            with self._send_lock:
                ws.send(
                    json.dumps(
                        {
                            "ticks": symbol,
                            "subscribe": 1,
                        }
                    )
                )
            return True
        except Exception as exc:
            print(
                f"[{symbol}] Subscription send failed: {exc}"
            )
            return False

    # ========================================================
    # SUBSCRIBE
    # ========================================================

    async def subscribe_candles(self, symbol, granularity=60):
        if self._closed:
            raise RuntimeError("PublicMarketClient is closed")

        start_worker = False
        already_subscribed = False

        with self._lock:
            if symbol in self._subscriptions:
                already_subscribed = True
            else:
                self._subscriptions[symbol] = int(granularity)
                self._current_candles.pop(symbol, None)

                if (
                    self._thread is None
                    or not self._thread.is_alive()
                ):
                    start_worker = True

        if already_subscribed:
            print(f"[{symbol}] Already subscribed - skipped.")
            return

        if start_worker:
            self._thread = threading.Thread(
                target=self._stream_worker,
                name="deriv-public-websocket",
                daemon=True,
            )
            self._thread.start()
        else:
            # Socket is already alive: add this symbol immediately.
            self._send_subscription(symbol)

        # Small stagger prevents a burst of subscription requests.
        await asyncio.sleep(0.25)

    # ========================================================
    # STREAM WORKER
    # ========================================================

    def _stream_worker(self):
        retry_delay = 2.0

        while not self._closed:
            with self._lock:
                symbols = list(self._subscriptions.keys())

            if not symbols:
                time.sleep(1)
                continue

            connected_at = time.monotonic()

            def on_open(sock):
                self._connected = True
                print(
                    f"[PUBLIC] WebSocket connected | "
                    f"subscribing {len(self._subscriptions)} symbols"
                )

                with self._lock:
                    current_symbols = list(self._subscriptions.keys())

                for item in current_symbols:
                    try:
                        with self._send_lock:
                            sock.send(
                                json.dumps(
                                    {
                                        "ticks": item,
                                        "subscribe": 1,
                                    }
                                )
                            )
                    except Exception as exc:
                        print(
                            f"[{item}] Initial subscription error: {exc}"
                        )

            def on_message(sock, message):
                try:
                    response = json.loads(message)

                    if "error" in response:
                        error = response["error"]
                        print(
                            "[PUBLIC] Deriv API error: "
                            f"{error.get('code', 'UNKNOWN')} - "
                            f"{error.get('message', 'Unknown error')}"
                        )
                        return

                    tick = response.get("tick")
                    if not tick:
                        return

                    symbol = tick.get("symbol")
                    quote = tick.get("quote")
                    epoch = tick.get("epoch")

                    if symbol is None or quote is None or epoch is None:
                        return

                    with self._lock:
                        granularity = self._subscriptions.get(symbol)

                    if granularity is None:
                        return

                    price = float(quote)
                    epoch = int(epoch)
                    candle_epoch = epoch - (epoch % granularity)

                    with self._lock:
                        current = self._current_candles.get(symbol)

                        is_new_candle = (
                            current is None
                            or current["epoch"] != candle_epoch
                        )

                        if is_new_candle:
                            current = {
                                "epoch": candle_epoch,
                                "open": price,
                                "high": price,
                                "low": price,
                                "close": price,
                                "granularity": granularity,
                            }
                            self._current_candles[symbol] = current
                        else:
                            current["high"] = max(
                                current["high"],
                                price,
                            )
                            current["low"] = min(
                                current["low"],
                                price,
                            )
                            current["close"] = price

                        candle_copy = dict(current)

                    callback = self.on_candle
                    loop = self._loop

                    if callback is None or loop is None:
                        return

                    candle_copy["is_new_candle"] = is_new_candle
                    candle_copy["tick_epoch"] = epoch

                    asyncio.run_coroutine_threadsafe(
                        callback(symbol, candle_copy),
                        loop,
                    )

                except Exception as exc:
                    print(
                        f"[PUBLIC] Candle message error: {exc}"
                    )

            def on_error(sock, error):
                print(
                    f"[PUBLIC] WebSocket error: {error}"
                )

            def on_close(sock, status_code, message):
                self._connected = False
                print(
                    f"[PUBLIC] WebSocket closed: "
                    f"{status_code} {message}"
                )

            ws = websocket.WebSocketApp(
                PUBLIC_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            with self._lock:
                self._ws = ws

            try:
                ws.run_forever(
                    ping_interval=15,
                    ping_timeout=8,
                    ping_payload="deriv-signal-bot",
                )
            except Exception as exc:
                if not self._closed:
                    print(
                        f"[PUBLIC] Stream exception: {exc}"
                    )
            finally:
                self._connected = False

                try:
                    ws.close()
                except Exception:
                    pass

                with self._lock:
                    if self._ws is ws:
                        self._ws = None

            if self._closed:
                break

            # If the connection was healthy for at least 30 seconds,
            # start the backoff again from 2 seconds. Otherwise increase
            # the delay so a temporary Deriv/Cloudflare 502 does not cause
            # a tight reconnect loop.
            stable = (time.monotonic() - connected_at) >= 30
            if stable:
                retry_delay = 2.0
            else:
                retry_delay = min(retry_delay * 2.0, 30.0)

            jitter = random.uniform(0.0, 1.5)
            wait_time = retry_delay + jitter

            print(
                f"[PUBLIC] Reconnecting in {wait_time:.1f} seconds..."
            )

            time.sleep(wait_time)

        self._connected = False

    # ========================================================
    # WAIT
    # ========================================================

    async def wait_until_disconnected(self):
        while not self._closed:
            await asyncio.sleep(1)

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(self):
        self._closed = True
        self._connected = False

        with self._lock:
            self._subscriptions.clear()
            self._current_candles.clear()
            ws = self._ws
            self._ws = None

        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


# ============================================================
# BACKWARD COMPATIBILITY
# ============================================================

DerivPublicClient = PublicMarketClient
