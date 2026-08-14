import asyncio
import json
import threading
import time

import websocket


# ============================================================
# DERIV PUBLIC WEBSOCKET
# ============================================================

PUBLIC_WS_URL = (
    "wss://api.derivws.com/trading/v1/options/ws/public"
)


class PublicMarketClient:

    def __init__(self, timeout=20):

        self.timeout = timeout

        self.on_candle = None

        self._connections = []
        self._threads = []

        self._closed = False
        self._connected = False

        self._loop = None

        # Symbols ambazo tayari zina stream
        self._subscribed_symbols = set()

        self._lock = threading.Lock()

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self):

        self._loop = asyncio.get_running_loop()

        self._closed = False
        self._connected = True

    # ========================================================
    # REQUEST
    # ========================================================

    def _request_sync(self, payload):

        ws = None

        try:

            ws = websocket.create_connection(
                PUBLIC_WS_URL,
                timeout=self.timeout,
            )

            ws.send(
                json.dumps(payload)
            )

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

        finally:

            if ws is not None:

                try:
                    ws.close()
                except Exception:
                    pass

    async def _request(self, payload):

        return await asyncio.to_thread(
            self._request_sync,
            payload,
        )

    # ========================================================
    # CANDLE HISTORY
    # ========================================================

    async def get_candle_history(
        self,
        symbol,
        granularity=60,
        count=200,
    ):

        response = await self._request(
            {
                "ticks_history": symbol,
                "end": "latest",
                "count": int(count),
                "style": "candles",
                "granularity": int(granularity),
            }
        )

        candles = response.get(
            "candles",
            [],
        )

        if not candles:

            raise RuntimeError(
                f"No candles received for {symbol}"
            )

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

    async def get_candles(
        self,
        symbol,
        granularity=60,
        count=200,
    ):

        return await self.get_candle_history(
            symbol,
            granularity,
            count,
        )

    async def get_ohlc(
        self,
        symbol,
        timeframe="1m",
        count=200,
    ):

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

            raise ValueError(
                f"Unsupported timeframe: {timeframe}"
            )

        return await self.get_candle_history(
            symbol,
            timeframe_map[timeframe],
            count,
        )

    # ========================================================
    # ACTIVE SYMBOLS
    # ========================================================

    async def get_active_symbols(self):

        response = await self._request(
            {
                "active_symbols": "brief"
            }
        )

        return response.get(
            "active_symbols",
            []
        )

    # ========================================================
    # CURRENT PRICE
    # ========================================================

    async def get_price(self, symbol):

        response = await self._request(
            {
                "ticks": symbol
            }
        )

        tick = response.get("tick")

        if not tick:

            raise RuntimeError(
                f"No tick received for {symbol}"
            )

        return {
            "symbol": symbol,
            "quote": float(
                tick["quote"]
            ),
            "epoch": int(
                tick["epoch"]
            ),
        }

    # ========================================================
    # SUBSCRIBE
    # ========================================================

    async def subscribe_candles(
        self,
        symbol,
        granularity=60,
    ):

        if self._closed:

            raise RuntimeError(
                "PublicMarketClient is closed"
            )

        # ----------------------------------------------------
        # USIRUDIE SUBSCRIPTION YA SYMBOL ILEILE
        # ----------------------------------------------------

        with self._lock:

            if symbol in self._subscribed_symbols:

                print(
                    f"[{symbol}] "
                    f"Already subscribed - skipped."
                )

                return

            self._subscribed_symbols.add(
                symbol
            )

        thread = threading.Thread(

            target=self._stream_worker,

            args=(
                symbol,
                int(granularity),
            ),

            daemon=True,
        )

        self._threads.append(
            thread
        )

        thread.start()

        await asyncio.sleep(
            0.5
        )

    # ========================================================
    # STREAM WORKER
    # ========================================================

    def _stream_worker(
        self,
        symbol,
        granularity,
    ):

        current_candle = None

        ws = None

        try:

            # =================================================
            # ON OPEN
            # =================================================

            def on_open(sock):

                request = {
                    "ticks": symbol,
                    "subscribe": 1,
                }

                sock.send(
                    json.dumps(request)
                )

                print(
                    f"[{symbol}] "
                    f"Tick stream connected"
                )

            # =================================================
            # ON MESSAGE
            # =================================================

            def on_message(
                sock,
                message,
            ):

                nonlocal current_candle

                try:

                    response = json.loads(
                        message
                    )

                    # -----------------------------------------
                    # ERROR
                    # -----------------------------------------

                    if "error" in response:

                        error = response["error"]

                        code = error.get(
                            "code",
                            "UNKNOWN",
                        )

                        text = error.get(
                            "message",
                            "Unknown error",
                        )

                        print(
                            f"[{symbol}] "
                            f"Deriv API error: "
                            f"{code} - {text}"
                        )

                        return

                    # -----------------------------------------
                    # TICK
                    # -----------------------------------------

                    tick = response.get(
                        "tick"
                    )

                    if not tick:
                        return

                    quote = tick.get(
                        "quote"
                    )

                    epoch = tick.get(
                        "epoch"
                    )

                    if (
                        quote is None
                        or epoch is None
                    ):
                        return

                    price = float(
                        quote
                    )

                    epoch = int(
                        epoch
                    )

                    # -----------------------------------------
                    # CANDLE BUCKET
                    # -----------------------------------------

                    candle_epoch = (
                        epoch
                        - (
                            epoch
                            % granularity
                        )
                    )

                    # -----------------------------------------
                    # NEW CANDLE
                    # -----------------------------------------

                    is_new_candle = (

                        current_candle is None

                        or

                        current_candle["epoch"]
                        != candle_epoch
                    )

                    if is_new_candle:

                        current_candle = {

                            "epoch":
                                candle_epoch,

                            "open":
                                price,

                            "high":
                                price,

                            "low":
                                price,

                            "close":
                                price,

                            "granularity":
                                granularity,
                        }

                    # -----------------------------------------
                    # UPDATE CURRENT CANDLE
                    # -----------------------------------------

                    else:

                        current_candle["high"] = max(
                            current_candle["high"],
                            price,
                        )

                        current_candle["low"] = min(
                            current_candle["low"],
                            price,
                        )

                        current_candle["close"] = (
                            price
                        )

                    # -----------------------------------------
                    # CALLBACK
                    # -----------------------------------------

                    callback = (
                        self.on_candle
                    )

                    loop = self._loop

                    if (
                        callback is None
                        or loop is None
                    ):
                        return

                    candle_copy = dict(
                        current_candle
                    )

                    candle_copy[
                        "is_new_candle"
                    ] = is_new_candle

                    candle_copy[
                        "tick_epoch"
                    ] = epoch

                    asyncio.run_coroutine_threadsafe(

                        callback(
                            symbol,
                            candle_copy,
                        ),

                        loop,
                    )

                except Exception as exc:

                    print(
                        f"[{symbol}] "
                        f"Candle message error: "
                        f"{exc}"
                    )

            # =================================================
            # ON ERROR
            # =================================================

            def on_error(
                sock,
                error,
            ):

                print(
                    f"[{symbol}] "
                    f"WebSocket error: "
                    f"{error}"
                )

            # =================================================
            # ON CLOSE
            # =================================================

            def on_close(
                sock,
                status_code,
                message,
            ):

                print(
                    f"[{symbol}] "
                    f"WebSocket closed: "
                    f"{status_code} "
                    f"{message}"
                )

            # =================================================
            # CREATE SOCKET
            # =================================================

            ws = websocket.WebSocketApp(

                PUBLIC_WS_URL,

                on_open=on_open,

                on_message=on_message,

                on_error=on_error,

                on_close=on_close,
            )

            self._connections.append(
                ws
            )

            # =================================================
            # RUN
            # =================================================

            while not self._closed:

                try:

                    ws.run_forever(
                        ping_interval=20,
                        ping_timeout=10,
                    )

                except Exception as exc:

                    if self._closed:
                        break

                    print(
                        f"[{symbol}] "
                        f"Stream exception: "
                        f"{exc}"
                    )

                if self._closed:
                    break

                print(
                    f"[{symbol}] "
                    f"Reconnecting in 2 seconds..."
                )

                time.sleep(2)

        finally:

            # -------------------------------------------------
            # Remove socket
            # -------------------------------------------------

            if ws is not None:

                try:
                    ws.close()
                except Exception:
                    pass

                try:
                    self._connections.remove(
                        ws
                    )
                except ValueError:
                    pass

            # -------------------------------------------------
            # Allow a future subscription
            # -------------------------------------------------

            with self._lock:

                self._subscribed_symbols.discard(
                    symbol
                )

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

            self._subscribed_symbols.clear()

        for ws in list(
            self._connections
        ):

            try:
                ws.close()
            except Exception:
                pass

        self._connections.clear()


# ============================================================
# BACKWARD COMPATIBILITY
# ============================================================

DerivPublicClient = PublicMarketClient
