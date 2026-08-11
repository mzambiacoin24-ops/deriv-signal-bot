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

        self._connections = []
        self._threads = []

        self._closed = False
        self._connected = False
        self._loop = None

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._connected = True

    def _request_sync(self, payload):
        ws = None

        try:
            ws = websocket.create_connection(
                PUBLIC_WS_URL,
                timeout=self.timeout
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

        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    async def _request(self, payload):
        return await asyncio.to_thread(
            self._request_sync,
            payload
        )

    async def get_candle_history(
        self,
        symbol,
        granularity=60,
        count=200
    ):
        response = await self._request({
            "ticks_history": symbol,
            "end": "latest",
            "count": int(count),
            "style": "candles",
            "granularity": int(granularity)
        })

        candles = response.get("candles", [])

        if not candles:
            raise RuntimeError(
                f"No candles received for {symbol}"
            )

        result = []

        for candle in candles:
            result.append({
                "epoch": int(candle["epoch"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "granularity": int(granularity)
            })

        return result

    async def get_candles(
        self,
        symbol,
        granularity=60,
        count=200
    ):
        return await self.get_candle_history(
            symbol,
            granularity,
            count
        )

    async def get_ohlc(
        self,
        symbol,
        timeframe="1m",
        count=200
    ):
        timeframe_map = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400
        }

        if timeframe not in timeframe_map:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}"
            )

        return await self.get_candle_history(
            symbol,
            timeframe_map[timeframe],
            count
        )

    async def subscribe_candles(
        self,
        symbol,
        granularity=60
    ):
        if self._closed:
            raise RuntimeError(
                "PublicMarketClient is closed"
            )

        thread = threading.Thread(
            target=self._stream_worker,
            args=(
                symbol,
                int(granularity)
            ),
            daemon=True
        )

        self._threads.append(thread)
        thread.start()

        await asyncio.sleep(0.2)

    def _stream_worker(
        self,
        symbol,
        granularity
    ):
        ws = None
        current_candle = None

        try:

            def on_open(sock):
                request = {
                    "ticks": symbol,
                    "subscribe": 1
                }

                sock.send(json.dumps(request))

                print(
                    f"[{symbol}] Tick stream connected"
                )

            def on_message(sock, message):
                nonlocal current_candle

                try:
                    response = json.loads(message)

                    if "error" in response:
                        error = response["error"]

                        print(
                            f"Deriv stream error "
                            f"for {symbol}: "
                            f"{error.get('code', 'UNKNOWN')} - "
                            f"{error.get('message', 'Unknown error')}"
                        )

                        return

                    tick = response.get("tick")

                    if not tick:
                        return

                    quote = tick.get("quote")
                    epoch = tick.get("epoch")

                    if quote is None or epoch is None:
                        return

                    price = float(quote)
                    epoch = int(epoch)

                    candle_epoch = (
                        epoch -
                        (epoch % granularity)
                    )

                    if (
                        current_candle is None
                        or
                        current_candle["epoch"]
                        != candle_epoch
                    ):
                        current_candle = {
                            "epoch": candle_epoch,
                            "open": price,
                            "high": price,
                            "low": price,
                            "close": price,
                            "granularity": granularity
                        }

                    else:
                        current_candle["high"] = max(
                            current_candle["high"],
                            price
                        )

                        current_candle["low"] = min(
                            current_candle["low"],
                            price
                        )

                        current_candle["close"] = price

                    callback = self.on_candle
                    loop = self._loop

                    if (
                        callback is not None
                        and loop is not None
                    ):
                        candle_copy = dict(
                            current_candle
                        )

                        asyncio.run_coroutine_threadsafe(
                            callback(
                                symbol,
                                candle_copy
                            ),
                            loop
                        )

                except Exception as exc:
                    print(
                        f"Candle message error "
                        f"for {symbol}: {exc}"
                    )

            def on_error(sock, error):
                print(
                    f"Deriv WebSocket error "
                    f"for {symbol}: {error}"
                )

            def on_close(
                sock,
                status_code,
                message
            ):
                print(
                    f"Deriv WebSocket closed "
                    f"for {symbol}: "
                    f"{status_code} {message}"
                )

            ws = websocket.WebSocketApp(
                PUBLIC_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            self._connections.append(ws)

            while not self._closed:

                try:
                    ws.run_forever(
                        ping_interval=20,
                        ping_timeout=10
                    )

                except Exception as exc:

                    if self._closed:
                        break

                    print(
                        f"Stream disconnected "
                        f"for {symbol}: {exc}"
                    )

                if self._closed:
                    break

                time.sleep(2)

        finally:

            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    async def wait_until_disconnected(self):

        while not self._closed:
            await asyncio.sleep(1)

    async def close(self):

        self._closed = True
        self._connected = False

        for ws in list(self._connections):

            try:
                ws.close()
            except Exception:
                pass

        self._connections.clear()

    async def get_price(self, symbol):

        response = await self._request({
            "ticks": symbol
        })

        tick = response.get("tick")

        if not tick:
            raise RuntimeError(
                f"No tick received for {symbol}"
            )

        return {
            "symbol": symbol,
            "quote": float(tick["quote"]),
            "epoch": int(tick["epoch"])
        }

    async def get_active_symbols(self):

        response = await self._request({
            "active_symbols": "brief"
        })

        return response.get(
            "active_symbols",
            []
        )


DerivPublicClient = PublicMarketClient
