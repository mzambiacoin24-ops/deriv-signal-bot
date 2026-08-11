from collections import deque


class SMCAnalyzer:

    def __init__(self, symbol, max_candles=250):
        self.symbol = symbol
        self.candles = deque(maxlen=max_candles)
        self.trend = None
        self.last_sweep = None
        self._last_signal_epoch = None

    def _update_trend(self):
        if len(self.candles) < 6:
            return

        recent = list(self.candles)[-6:]

        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]

        higher_high = highs[-1] > highs[-3]
        higher_low = lows[-1] > lows[-3]

        lower_high = highs[-1] < highs[-3]
        lower_low = lows[-1] < lows[-3]

        if higher_high and higher_low:
            self.trend = "up"

        elif lower_high and lower_low:
            self.trend = "down"

    def _detect_sweep(self, candle):
        if len(self.candles) < 5:
            return None

        previous = list(self.candles)[-5:]

        previous_high = max(
            c["high"] for c in previous
        )

        previous_low = min(
            c["low"] for c in previous
        )

        swept_high = (
            candle["high"] > previous_high
            and candle["close"] < previous_high
        )

        swept_low = (
            candle["low"] < previous_low
            and candle["close"] > previous_low
        )

        if swept_high and not swept_low:
            return "high"

        if swept_low and not swept_high:
            return "low"

        return None

    def _find_order_block(self, direction):

        candles = list(self.candles)

        if len(candles) < 2:
            return None

        for candle in reversed(candles[:-1]):

            body = (
                candle["close"]
                - candle["open"]
            )

            if direction == "up" and body < 0:

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

            if direction == "down" and body > 0:

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

        return None

    def _detect_choch(self):

        if len(self.candles) < 8:
            return None

        candles = list(self.candles)

        current = candles[-1]

        window = candles[-7:-1]

        previous_high = max(
            c["high"] for c in window
        )

        previous_low = min(
            c["low"] for c in window
        )

        if current["close"] > previous_high:
            return "up"

        if current["close"] < previous_low:
            return "down"

        return None

    def add_candle(self, candle):

        required = (
            "open",
            "high",
            "low",
            "close"
        )

        for key in required:

            if key not in candle:

                raise ValueError(
                    "Candle must contain "
                    "open, high, low and close"
                )

        c = {
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "epoch": candle.get("epoch"),
        }

        epoch = c.get("epoch")

        if self.candles and epoch is not None:

            last_epoch = (
                self.candles[-1].get("epoch")
            )

            if last_epoch == epoch:

                self.candles[-1] = c

                return None

        sweep = self._detect_sweep(c)

        if sweep is not None:

            self.last_sweep = sweep

        self.candles.append(c)

        self._update_trend()

        choch = self._detect_choch()

        if choch is None:
            return None

        ob = self._find_order_block(
            choch
        )

        if ob is None:
            return None

        if (
            epoch is not None
            and epoch == self._last_signal_epoch
        ):
            return None

        self._last_signal_epoch = epoch

        return {
            "direction": choch,
            "ob": ob,
            "epoch": epoch,
            "symbol": self.symbol,
      }
