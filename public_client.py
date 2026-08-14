import asyncio
import json
import threading
import time

import websocket


# ============================================================
# DERIV PUBLIC MARKET DATA
# ============================================================

PUBLIC_WS_URL = (
    "wss://ws.binaryws.com/websockets/v3"
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

        # Prevent duplicate subscriptions
        self._subscribed_symbols = set()

        # Lock protects subscription set
        self._subscription_lock = threading.Lock()

    # ========================================================
    # CONNECT
    # ========================================================

    async def connect(self):

        self._loop = asyncio.get_running_loop()

        self._closed = False
        self._connected = True

    # ========================================================
    # SYNC REQUEST
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

    # ========================================================
    # ASYNC REQUEST
    # ========================================================

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
                "epoch": int(candle["epoch"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "granularity": int(granularity),
            }
            for candle in candles
        ]

    # ========================================================
    # ALIASES
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
            "3m": 180,
            "5m": 300,
            "10m": 600,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "8h": 28800,
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
    # GET ACTIVE SYMBOLS
    # ========================================================

    async def get_active_symbols(self):

        response = await self._request(
            {
                "active_symbols": "brief",
            }
        )

        return response.get(
            "active_symbols",
            [],
        )

    # ========================================================
    # FIND SYNTHETIC INDEX FEEDS
    #
    # Returns both normal and 1-second feeds
    # when Deriv exposes them.
    # ========================================================

    async def get_volatility_feeds(self):

        symbols = await self.get_active_symbols()

        feeds = []

        for item in symbols:

            symbol = (
                item.get("symbol")
                or item.get("underlying_symbol")
                or ""
            )

            name = (
                item.get("display_name")
                or item.get("underlying_symbol_name")
                or ""
            )

            if not symbol:
                continue

            name_lower = name.lower()

            if "volatility" not in name_lower:
                continue

            feeds.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": item.get(
                        "market",
                        "",
                    ),
                    "submarket": item.get(
                        "submarket",
                        "",
                    ),
                }
            )

        return feeds

    # ========================================================
    # FIND FEEDS FOR ONE VOLATILITY INDEX
    #
    # Example:
    #
    # R_50
    # 1HZ50V
    #
    # Both are returned if available.
    # ========================================================

    async def get_index_feeds(
        self,
        index_number,
    ):

        feeds = await self.get_volatility_feeds()

        target = (
            f"volatility {index_number} index"
        )

        target_1s = (
            f"volatility {index_number} (1s) index"
        )

        result = []

        for feed in feeds:

            name = feed["name"].lower()

            if (
                name == target
                or name == target_1s
            ):

                result.append(feed)

        return result

    # ========================================================
    # GET CURRENT PRICE
    # ========================================================

    async def get_price(
        self,
        symbol,
    ):

        response = await self._request(
            {
                "ticks": symbol,
            }
        )

        tick = response.get(
            "tick"
        )

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
    # SUBSCRIBE CANDLES
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
        # IMPORTANT:
        # Do not subscribe to exactly the same symbol twice.
        # ----------------------------------------------------

        with self._subscription_lock:

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
            0.2
        )

    # ========================================================
    # STREAM WORKER
    # ========================================================

    def _stream_worker(
        self,
        symbol,
        granularity,
    ):

        ws = None

        current_candle = None

        try:

            # ------------------------------------------------
            # OPEN
            # ------------------------------------------------

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

            # ------------------------------------------------
            # MESSAGE
            # ------------------------------------------------

            def on_message(
                sock,
                message,
            ):

                nonlocal current_candle

                try:

                    response = json.loads(
                        message
                    )

                    # ----------------------------------------
                    # API ERROR
                    # ----------------------------------------

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

                        # ------------------------------------
                        # Already subscribed is NOT fatal.
                        # ------------------------------------

                        if code == "AlreadySubscribed":

                            print(
                                f"[{symbol}] "
                                f"AlreadySubscribed - "
                                f"stream already exists."
                            )

                            return

                        print(
                            f"[{symbol}] "
                            f"Deriv stream error: "
                            f"{code} - {text}"
                        )

                        return

                    # ----------------------------------------
                    # TICK
                    # ----------------------------------------

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

                    candle_epoch = (
                        epoch
                        - (
                            epoch
                            % granularity
                        )
                    )

                    # ----------------------------------------
                    # NEW CANDLE
                    # ----------------------------------------

                    is_new_candle = (

                        current_candle is None

                        or

                        current_candle[
                            "epoch"
                        ]
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

                    else:

                        current_candle[
                            "high"
                        ] = max(

                            current_candle[
                                "high"
                            ],

                            price,
                        )

                        current_candle[
                            "low"
                        ] = min(

                            current_candle[
                                "low"
                            ],

                            price,
                        )

                        current_candle[
                            "close"
                        ] = price

                    # ----------------------------------------
                    # CALLBACK
                    # ----------------------------------------

                    callback = (
                        self.on_candle
                    )

                    loop = (
                        self._loop
                    )

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
                    ] = (
                        is_new_candle
                    )

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

            # ------------------------------------------------
            # ERROR
            # ------------------------------------------------

            def on_error(
                sock,
                error,
            ):

                print(
                    f"[{symbol}] "
                    f"Deriv WebSocket error: "
                    f"{error}"
                )

            # ------------------------------------------------
            # CLOSE
            # ------------------------------------------------

            def on_close(
                sock,
                status_code,
                message,
            ):

                print(
                    f"[{symbol}] "
                    f"Deriv WebSocket closed: "
                    f"{status_code} "
                    f"{message}"
                )

            # ------------------------------------------------
            # WEBSOCKET
            # ------------------------------------------------

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

            # ------------------------------------------------
            # RECONNECT LOOP
            # ------------------------------------------------

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
                        f"Stream disconnected: "
                        f"{exc}"
                    )

                if self._closed:
                    break

                print(
                    f"[{symbol}] "
                    f"Reconnecting in 2 seconds..."
                )

                time.sleep(
                    2
                )

        finally:

            # Remove subscription status
            with self._subscription_lock:

                self._subscribed_symbols.discard(
                    symbol
                )

            if ws is not None:

                try:
                    ws.close()
                except Exception:
                    pass

            try:
                self._connections.remove(
                    ws
                )
            except Exception:
                pass

    # ========================================================
    # WAIT
    # ========================================================

    async def wait_until_disconnected(
        self,
    ):

        while not self._closed:

            await asyncio.sleep(
                1
            )

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(
        self,
    ):

        self._closed = True

        self._connected = False

        with self._subscription_lock:

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
