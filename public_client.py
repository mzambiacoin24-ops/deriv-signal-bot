import json
import time
import websocket


# Deriv public WebSocket.
# Hii ni market-data connection tu; haihitaji Telegram token wala MT5 token.
WS_URL = "wss://ws.binaryws.com/websockets/v3"


class DerivPublicClient:
    def __init__(self, timeout=15):
        self.timeout = timeout

    def _request(self, payload):
        ws = None

        try:
            ws = websocket.create_connection(
                WS_URL,
                timeout=self.timeout
            )

            ws.send(json.dumps(payload))

            while True:
                raw = ws.recv()

                if not raw:
                    continue

                data = json.loads(raw)

                if "error" in data:
                    error = data["error"]
                    raise RuntimeError(
                        f"Deriv API error: "
                        f"{error.get('code', 'UNKNOWN')} - "
                        f"{error.get('message', 'Unknown error')}"
                    )

                return data

        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def get_active_symbols(self):
        """
        Returns currently available Deriv symbols.
        """

        response = self._request({
            "active_symbols": "brief"
        })

        return response.get("active_symbols", [])

    def get_price(self, symbol):
        """
        Returns the latest price for a symbol.
        """

        response = self._request({
            "ticks": symbol
        })

        tick = response.get("tick")

        if not tick:
            raise RuntimeError(
                f"No tick data received for {symbol}"
            )

        return float(tick["quote"])

    def get_candles(
        self,
        symbol,
        granularity=60,
        count=200
    ):
        """
        Get historical OHLC candles.

        granularity:
            60  = 1 minute
            300 = 5 minutes
            900 = 15 minutes
            1800 = 30 minutes
            3600 = 1 hour
        """

        response = self._request({
            "ticks_history": symbol,
            "end": "latest",
            "count": int(count),
            "style": "candles",
            "granularity": int(granularity)
        })

        candles = response.get("candles", [])

        if not candles:
            raise RuntimeError(
                f"No candle data received for {symbol}"
            )

        result = []

        for candle in candles:
            result.append({
                "epoch": int(candle["epoch"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })

        return result

    def get_ohlc(
        self,
        symbol,
        timeframe="1m",
        count=200
    ):
        """
        Easier interface for the signal engine.

        Supported timeframes:
            1m
            5m
            15m
            30m
            1h
            4h
            1d
        """

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

        return self.get_candles(
            symbol=symbol,
            granularity=timeframe_map[timeframe],
            count=count
        )


# ---------------------------------------------------------
# Simple helper functions
# ---------------------------------------------------------

def get_candles(symbol, timeframe="1m", count=200):
    """
    Standalone function for easy importing.
    """

    client = DerivPublicClient()

    return client.get_ohlc(
        symbol=symbol,
        timeframe=timeframe,
        count=count
    )


def get_price(symbol):
    """
    Standalone latest-price function.
    """

    client = DerivPublicClient()

    return client.get_price(symbol)


def get_active_symbols():
    """
    Standalone active-symbols function.
    """

    client = DerivPublicClient()

    return client.get_active_symbols()


# ---------------------------------------------------------
# Local test
# ---------------------------------------------------------

if __name__ == "__main__":

    print("Connecting to Deriv public market data...")

    client = DerivPublicClient()

    try:

        symbol = "1HZ10V"

        price = client.get_price(symbol)

        print(f"Current price: {price}")

        candles = client.get_ohlc(
            symbol=symbol,
            timeframe="1m",
            count=10
        )

        print("\nLast candles:")

        for candle in candles[-5:]:
            print(candle)

        print("\nPUBLIC CLIENT TEST: OK")

    except Exception as e:

        print("\nPUBLIC CLIENT TEST FAILED")
        print(str(e))
