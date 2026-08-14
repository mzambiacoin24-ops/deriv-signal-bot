import asyncio
import json
import threading
import time

import websocket


PUBLIC_WS_URL = (
    "wss://api.derivws.com/trading/v1/options/ws/public"
)


class PublicMarketClient:

    def __init__(self, timeout=15):
        self.timeout = timeout

        self.on_candle = None

        self._loop = None
        self._closed = False
        self._connected = False

        # ONE WebSocket connection only
        self._ws = None
        self._thread = None

        self._lock = threading.RLock()

        # Requested subscriptions:
        # {(symbol, granularity)}
        self._subscriptions = set()

        # Latest candle for each symbol/timeframe
        self._current_candles = {}

        # Historical latest candles
        self._history_latest = {}

        # Connection state
        self._connected_event = threading.Event()

        # Stop event
        self._stop_event = threading.Event()


    async def connect(self):
        self._loop = asyncio.get_running_loop()

        self._closed = False
        self._connected = False

        self._stop_event.clear()
        self._connected_event.clear()

        if (
            self._thread is None
            or not self._thread.is_alive()
        ):

            self._thread = threading.Thread(
                target=self._stream_worker,
                daemon=True,
                name="deriv-public-websocket",
            )

            self._thread.start()

        # Give the worker a moment to establish connection
        await asyncio.sleep(0.5)


    # ======================================================
    # ONE WEBSOCKET WORKER
    # ======================================================

    def _stream_worker(self):

        reconnect_delay = 2
        max_reconnect_delay = 30

        while not self._stop_event.is_set():

            ws = None

            try:

                ws = websocket.WebSocketApp(
                    PUBLIC_WS_URL,

                    on_open=self._on_open,

                    on_message=self._on_message,

                    on_error=self._on_error,

                    on_close=self._on_close,

                    on_pong=self._on_pong,
                )

                with self._lock:
                    self._ws = ws

                print(
                    "Deriv WebSocket connecting..."
                )

                # IMPORTANT:
                #
                # Do not use a very aggressive ping timeout.
                #
                # Deriv recommends keeping long-lived
                # WebSocket connections alive with periodic
                # ping messages.
                #
                ws.run_forever(
                    ping_interval=45,
                    ping_timeout=15,
                    ping_payload="signal-bot",
                )

            except Exception as exc:

                if not self._stop_event.is_set():

                    print(
                        f"Deriv WebSocket worker error: {exc}"
                    )

            finally:

                with self._lock:

                    if self._ws is ws:
                        self._ws = None

                    self._connected = False

                self._connected_event.clear()

                if ws is not None:

                    try:
                        ws.close()

                    except Exception:
                        pass


            if self._stop_event.is_set():
                break


            print(
                f"Deriv WebSocket reconnecting "
                f"in {reconnect_delay}s..."
            )

            time.sleep(
                reconnect_delay
            )

            reconnect_delay = min(
                reconnect_delay * 2,
                max_reconnect_delay,
            )


    # ======================================================
    # ON OPEN
    # ======================================================

    def _on_open(self, ws):

        with self._lock:

            self._connected = True

        self._connected_event.set()

        print(
            "Deriv WebSocket connected "
            "(ONE connection)"
        )

        # Re-subscribe everything after reconnect
        self._send_all_subscriptions()


    # ======================================================
    # SEND ALL SUBSCRIPTIONS
    # ======================================================

    def _send_all_subscriptions(self):

        with self._lock:

            ws = self._ws

            subscriptions = list(
                self._subscriptions
            )

        if ws is None:
            return

        if not subscriptions:
            return

        symbols = sorted(
            {
                symbol
                for symbol, _ in subscriptions
            }
        )

        # Deriv supports ticks with multiple symbols
        # on one public connection.
        #
        # We subscribe to all required symbols together.
        try:

            ws.send(
                json.dumps(
                    {
                        "ticks": symbols,
                        "subscribe": 1,
                    }
                )
            )

            print(
                "Tick subscriptions sent: "
                + ", ".join(symbols)
            )

        except Exception as exc:

            print(
                f"Failed to subscribe ticks: {exc}"
            )


    # ======================================================
    # ON MESSAGE
    # ======================================================

    def _on_message(
        self,
        ws,
        message,
    ):

        try:

            response = json.loads(
                message
            )

        except Exception as exc:

            print(
                f"Invalid Deriv message: {exc}"
            )

            return


        # --------------------------------------------------
        # API ERROR
        # --------------------------------------------------

        if "error" in response:

            error = response.get(
                "error",
                {}
            )

            print(
                "Deriv API error: "
                f"{error.get('code', 'UNKNOWN')} - "
                f"{error.get('message', 'Unknown error')}"
            )

            return


        # --------------------------------------------------
        # TICK
        # --------------------------------------------------

        tick = response.get(
            "tick"
        )

        if not tick:
            return


        symbol = tick.get(
            "symbol"
        )

        quote = tick.get(
            "quote"
        )

        epoch = tick.get(
            "epoch"
        )


        if (
            symbol is None
            or quote is None
            or epoch is None
        ):
            return


        try:

            price = float(
                quote
            )

            epoch = int(
                epoch
            )

        except (
            TypeError,
            ValueError,
        ):

            return


        # --------------------------------------------------
        # UPDATE EVERY REQUESTED TIMEFRAME
        # --------------------------------------------------

        with self._lock:

            subscriptions = [
                granularity
                for sub_symbol, granularity
                in self._subscriptions
                if sub_symbol == symbol
            ]


        for granularity in subscriptions:

            self._process_tick(
                symbol,
                granularity,
                price,
                epoch,
            )


    # ======================================================
    # PROCESS TICK INTO CANDLE
    # ======================================================

    def _process_tick(
        self,
        symbol,
        granularity,
        price,
        epoch,
    ):

        candle_epoch = (
            epoch
            - (
                epoch
                % granularity
            )
        )

        key = (
            symbol,
            granularity,
        )


        with self._lock:

            current = (
                self._current_candles.get(
                    key
                )
            )


            # --------------------------------------------------
            # FIRST LIVE TICK
            # --------------------------------------------------

            if current is None:

                history_candle = (
                    self._history_latest.get(
                        key
                    )
                )


                if (
                    history_candle
                    and
                    history_candle["epoch"]
                    == candle_epoch
                ):

                    current = dict(
                        history_candle
                    )

                    # Update with live tick
                    current["high"] = max(
                        current["high"],
                        price,
                    )

                    current["low"] = min(
                        current["low"],
                        price,
                    )

                    current["close"] = price

                    self._current_candles[
                        key
                    ] = current

                    # Do NOT send the currently open
                    # historical candle yet.
                    return


                current = {
                    "epoch": candle_epoch,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "granularity": granularity,
                }

                self._current_candles[
                    key
                ] = current

                return


            # --------------------------------------------------
            # SAME CANDLE
            # --------------------------------------------------

            if (
                current["epoch"]
                == candle_epoch
            ):

                current["high"] = max(
                    current["high"],
                    price,
                )

                current["low"] = min(
                    current["low"],
                    price,
                )

                current["close"] = price

                return


            # --------------------------------------------------
            # NEW CANDLE
            #
            # The previous candle is now CLOSED.
            # THIS is what we send to SMC.
            # --------------------------------------------------

            closed_candle = dict(
                current
            )

            closed_candle[
                "granularity"
            ] = granularity

            closed_candle[
                "is_new_candle"
            ] = True

            closed_candle[
                "is_closed"
            ] = True

            closed_candle[
                "tick_epoch"
            ] = epoch


            # Create new live candle
            new_candle = {
                "epoch": candle_epoch,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "granularity": granularity,
            }

            self._current_candles[
                key
            ] = new_candle


        # Callback OUTSIDE the lock
        self._dispatch_candle(
            symbol,
            closed_candle,
        )


    # ======================================================
    # DISPATCH CANDLE TO SIGNAL BOT
    # ======================================================

    def _dispatch_candle(
        self,
        symbol,
        candle,
    ):

        callback = self.on_candle
        loop = self._loop

        if (
            callback is None
            or loop is None
        ):
            return


        try:

            future = (
                asyncio.run_coroutine_threadsafe(
                    callback(
                        symbol,
                        candle,
                    ),
                    loop,
                )
            )

            # We intentionally do not block here.
            # The asyncio loop handles the callback.

            future.add_done_callback(
                self._callback_done
            )

        except Exception as exc:

            print(
                f"Candle callback error "
                f"for {symbol}: {exc}"
            )


    def _callback_done(
        self,
        future,
    ):

        try:
            future.result()

        except Exception as exc:

            print(
                f"Signal callback error: {exc}"
            )


    # ======================================================
    # ON ERROR
    # ======================================================

    def _on_error(
        self,
        ws,
        error,
    ):

        print(
            f"Deriv WebSocket error: {error}"
        )


    # ======================================================
    # ON CLOSE
    # ======================================================

    def _on_close(
        self,
        ws,
        status_code,
        message,
    ):

        with self._lock:

            self._connected = False

        self._connected_event.clear()

        print(
            "Deriv WebSocket closed: "
            f"{status_code} {message}"
        )


    # ======================================================
    # ON PONG
    # ======================================================

    def _on_pong(
        self,
        ws,
        message,
    ):

        # Keep this quiet.
        # We don't want logs every 45 seconds.

        pass


    # ======================================================
    # HISTORICAL CANDLES
    # ======================================================

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
                "granularity": int(
                    granularity
                ),
            }
        )


        candles = response.get(
            "candles",
            []
        )


        if not candles:

            raise RuntimeError(
                f"No candles received "
                f"for {symbol}"
            )


        result = []

        for candle in candles:

            result.append(
                {
                    "epoch": int(
                        candle["epoch"]
                    ),

                    "open": float(
                        candle["open"]
                    ),

                    "high": float(
                        candle["high"]
                    ),

                    "low": float(
                        candle["low"]
                    ),

                    "close": float(
                        candle["close"]
                    ),

                    "granularity": int(
                        granularity
                    ),
                }
            )


        # Save latest historical candle.
        #
        # This prevents the first live tick from
        # creating a duplicate candle.

        if result:

            key = (
                symbol,
                int(granularity),
            )

            with self._lock:

                self._history_latest[
                    key
                ] = dict(
                    result[-1]
                )


        return result


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
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }


        if timeframe not in timeframe_map:

            raise ValueError(
                f"Unsupported timeframe: "
                f"{timeframe}"
            )


        return await self.get_candle_history(
            symbol,
            timeframe_map[timeframe],
            count,
        )


    # ======================================================
    # SUBSCRIBE
    # ======================================================

    async def subscribe_candles(
        self,
        symbol,
        granularity=60,
    ):

        if self._closed:

            raise RuntimeError(
                "PublicMarketClient is closed"
            )


        key = (
            symbol,
            int(granularity),
        )


        with self._lock:

            already_subscribed = (
                key in self._subscriptions
            )

            self._subscriptions.add(
                key
            )


            ws = self._ws

            connected = (
                self._connected
                and ws is not None
            )


        if already_subscribed:
            return


        # If connection is already open,
        # resubscribe tick stream.
        if connected:

            try:

                with self._lock:

                    symbols = sorted(
                        {
                            sub_symbol
                            for sub_symbol, _
                            in self._subscriptions
                        }
                    )

                ws.send(
                    json.dumps(
                        {
                            "ticks": symbols,
                            "subscribe": 1,
                        }
                    )
                )

            except Exception as exc:

                print(
                    f"Subscription send error: {exc}"
                )


        await asyncio.sleep(
            0.1
        )


    # ======================================================
    # REQUEST/RESPONSE
    #
    # Used for historical data and active symbols.
    # Each request gets a SHORT-LIVED connection.
    #
    # Live tick streaming still uses ONE persistent
    # connection.
    # ======================================================

    def _request_sync(
        self,
        payload,
    ):

        ws = None

        try:

            ws = websocket.create_connection(
                PUBLIC_WS_URL,
                timeout=self.timeout,
            )


            ws.send(
                json.dumps(
                    payload
                )
            )


            while True:

                raw = ws.recv()

                if not raw:
                    continue


                response = json.loads(
                    raw
                )


                if "error" in response:

                    error = response[
                        "error"
                    ]

                    raise RuntimeError(
                        "Deriv API error: "
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


    async def _request(
        self,
        payload,
    ):

        return await asyncio.to_thread(
            self._request_sync,
            payload,
        )


    # ======================================================
    # CURRENT PRICE
    # ======================================================

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
                f"No tick received "
                f"for {symbol}"
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


    # ======================================================
    # ACTIVE SYMBOLS
    # ======================================================

    async def get_active_symbols(
        self,
    ):

        response = await self._request(
            {
                "active_symbols": "brief"
            }
        )


        return response.get(
            "active_symbols",
            []
        )


    # ======================================================
    # WAIT
    # ======================================================

    async def wait_until_disconnected(
        self,
    ):

        while not self._closed:

            await asyncio.sleep(
                1
            )


    # ======================================================
    # CLOSE
    # ======================================================

    async def close(
        self,
    ):

        self._closed = True

        self._stop_event.set()

        self._connected = False

        self._connected_event.clear()


        with self._lock:

            ws = self._ws

            self._ws = None

            self._subscriptions.clear()


        if ws is not None:

            try:
                ws.close()

            except Exception:
                pass


        # Give worker a short moment to finish.
        if (
            self._thread
            and self._thread.is_alive()
        ):

            await asyncio.sleep(
                0.2
            )


        self._thread = None


    # ======================================================
    # ALIAS
    # ======================================================


DerivPublicClient = PublicMarketClient
