import json
import websocket


# ============================================================
# DERIV PUBLIC MARKET DATA CLIENT
# ============================================================
#
# Kazi ya file hii:
# - Kupata active symbols
# - Kupata historical candles
# - Kupata live/latest tick
#
# HAIHUSIKI na:
# - Telegram
# - Account balance
# - Open positions
# - MT5
# - Trading orders
#
# Account authentication itakuwa kwenye deriv_auth.py
# ============================================================


PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


class DerivPublicClient:

    def __init__(self, timeout=15):
        self.timeout = timeout

    # --------------------------------------------------------
    # CONNECT + REQUEST
    # --------------------------------------------------------

    def _request(self, payload):

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

                # Deriv API error
                if "error" in response:

                    error = response["error"]

                    raise RuntimeError(
                        f"Deriv API error: "
                        f"{error.get('code', 'UNKNOWN')} - "
                        f"{error.get('message', 'Unknown error')}"
                    )

                return response

        except Exception as e:

            raise RuntimeError(
                f"Deriv public connection failed: {e}"
            )

        finally:

            if ws is not None:

                try:
                    ws.close()
                except Exception:
                    pass

    # --------------------------------------------------------
    # ACTIVE SYMBOLS
    # --------------------------------------------------------

    def get_active_symbols(self):

        response = self._request({
            "active_symbols": "brief"
        })

        symbols = response.get(
            "active_symbols",
            []
        )

        return symbols

    # --------------------------------------------------------
    # LATEST PRICE
    # --------------------------------------------------------

    def get_price(self, symbol):

        response = self._request({
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

    # --------------------------------------------------------
    # HISTORICAL CANDLES
    # --------------------------------------------------------

    def get_candles(
        self,
        symbol,
        granularity=60,
        count=200
    ):

        response = self._request({

            "ticks_history": symbol,

            "end": "latest",

            "count": int(count),

            "style": "candles",

            "granularity": int(granularity),

            "subscribe": 0
        })

        candles = response.get(
            "candles",
            []
        )

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

                "close": float(candle["close"])
            })

        return result

    # --------------------------------------------------------
    # TIMEFRAME HELPER
    # --------------------------------------------------------

    def get_ohlc(
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

        return self.get_candles(

            symbol=symbol,

            granularity=timeframe_map[timeframe],

            count=count
        )


# ============================================================
# SIMPLE FUNCTIONS
# ============================================================

def get_price(symbol):

    client = DerivPublicClient()

    return client.get_price(symbol)


def get_candles(
    symbol,
    timeframe="1m",
    count=200
):

    client = DerivPublicClient()

    return client.get_ohlc(

        symbol=symbol,

        timeframe=timeframe,

        count=count
    )


def get_active_symbols():

    client = DerivPublicClient()

    return client.get_active_symbols()


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print(
        "======================================"
    )

    print(
        "DERIV PUBLIC CLIENT TEST"
    )

    print(
        "======================================"
    )

    client = DerivPublicClient()

    try:

        # Test symbol
        symbol = "1HZ10V"

        # Latest price
        price = client.get_price(symbol)

        print(
            f"\nSymbol: {price['symbol']}"
        )

        print(
            f"Price: {price['quote']}"
        )

        print(
            f"Epoch: {price['epoch']}"
        )

        # Historical candles
        candles = client.get_ohlc(

            symbol=symbol,

            timeframe="1m",

            count=10
        )

        print(
            "\nLast candles:"
        )

        for candle in candles[-5:]:

            print(candle)

        print(
            "\nPUBLIC CLIENT: OK"
        )

    except Exception as e:

        print(
            "\nPUBLIC CLIENT: FAILED"
        )

        print(
            str(e)
    )
