from collections import deque


class SMCAnalyzer:
    def __init__(self, symbol, lookback=2, history=300):
        self.symbol = symbol
        self.lookback = lookback
        self.candles = deque(maxlen=history)

        self.trend = None

        self.last_swing_high = None
        self.last_swing_low = None

        self.swing_highs = deque(maxlen=15)
        self.swing_lows = deque(maxlen=15)

        self.pending_ob = None
        self.pending_fvg = None

        self.last_event = None

        self.last_sweep = None
        self.sweep_age = None

        # Market structure state
        self.last_structure_high = None
        self.previous_structure_high = None

        self.last_structure_low = None
        self.previous_structure_low = None

        self.structure_highs = deque(maxlen=10)
        self.structure_lows = deque(maxlen=10)

    def add_candle(self, candle):
        self.candles.append(candle)

        if self.sweep_age is not None:
            self.sweep_age += 1

            if self.sweep_age > 6:
                self.last_sweep = None
                self.sweep_age = None

        entry_signal = None

        # Existing pending order block is waiting for retest
        if self.pending_ob and self._price_in_ob(candle):
            entry_signal = {
                "direction": self.pending_ob["direction"],
                "ob": dict(self.pending_ob),
                "fvg": (
                    dict(self.pending_fvg)
                    if self.pending_fvg
                    else None
                ),
            }

            self.pending_ob = None
            self.pending_fvg = None

        # Detect liquidity sweep before registering the new swing
        self._detect_liquidity_sweep(candle)

        # Detect confirmed swing
        self._detect_confirmed_swing()

        return entry_signal

    def _detect_liquidity_sweep(self, candle):
        if self.last_swing_high is not None:
            if (
                candle["high"] > self.last_swing_high
                and candle["close"] < self.last_swing_high
            ):
                self.last_sweep = "high"
                self.sweep_age = 0
                self.last_event = "SWEEP_HIGH"

        if self.last_swing_low is not None:
            if (
                candle["low"] < self.last_swing_low
                and candle["close"] > self.last_swing_low
            ):
                self.last_sweep = "low"
                self.sweep_age = 0
                self.last_event = "SWEEP_LOW"

    def _detect_confirmed_swing(self):
        n = len(self.candles)

        idx = n - 1 - self.lookback

        if idx - self.lookback < 0:
            return

        candles = list(self.candles)

        center = candles[idx]

        left = candles[
            idx - self.lookback:idx
        ]

        right = candles[
            idx + 1:idx + 1 + self.lookback
        ]

        if len(right) < self.lookback:
            return

        is_swing_high = (
            all(
                center["high"] > c["high"]
                for c in left
            )
            and
            all(
                center["high"] > c["high"]
                for c in right
            )
        )

        is_swing_low = (
            all(
                center["low"] < c["low"]
                for c in left
            )
            and
            all(
                center["low"] < c["low"]
                for c in right
            )
        )

        if is_swing_high:
            self._register_swing_high(center)

        if is_swing_low:
            self._register_swing_low(center)

    def _register_swing_high(self, candle):
        high = candle["high"]

        self.last_swing_high = high
        self.swing_highs.append(high)

        self.previous_structure_high = (
            self.last_structure_high
        )

        self.last_structure_high = high

        self.structure_highs.append(high)

        self._evaluate_market_structure()

    def _register_swing_low(self, candle):
        low = candle["low"]

        self.last_swing_low = low
        self.swing_lows.append(low)

        self.previous_structure_low = (
            self.last_structure_low
        )

        self.last_structure_low = low

        self.structure_lows.append(low)

        self._evaluate_market_structure()

    def _evaluate_market_structure(self):
        if (
            self.previous_structure_high is None
            or self.last_structure_high is None
            or self.previous_structure_low is None
            or self.last_structure_low is None
        ):
            return

        higher_high = (
            self.last_structure_high
            > self.previous_structure_high
        )

        higher_low = (
            self.last_structure_low
            > self.previous_structure_low
        )

        lower_high = (
            self.last_structure_high
            < self.previous_structure_high
        )

        lower_low = (
            self.last_structure_low
            < self.previous_structure_low
        )

        # Confirmed bullish structure:
        # HH + HL
        if higher_high and higher_low:
            if self.trend == "down":
                self._trigger_choch("up")
            else:
                self.trend = "up"
                self.last_event = "BOS_UP"

            return

        # Confirmed bearish structure:
        # LH + LL
        if lower_high and lower_low:
            if self.trend == "up":
                self._trigger_choch("down")
            else:
                self.trend = "down"
                self.last_event = "BOS_DOWN"

            return

        # A single higher high or lower low is NOT
        # enough to change the confirmed trend.
        if higher_high:
            if self.trend == "up":
                self.last_event = "HH"

        if higher_low:
            if self.trend == "up":
                self.last_event = "HL"

        if lower_high:
            if self.trend == "down":
                self.last_event = "LH"

        if lower_low:
            if self.trend == "down":
                self.last_event = "LL"

    def _trigger_choch(self, new_direction):
        self.trend = new_direction

        self.last_event = (
            "CHOCH_" + new_direction.upper()
        )

        # Create a fresh setup only after the new
        # market structure has been confirmed.
        self.pending_ob = self._find_order_block(
            new_direction
        )

        self.pending_fvg = self._find_fvg(
            new_direction
        )

    def _find_order_block(self, direction):
        candles = list(self.candles)

        want_bearish_candle = (
            direction == "up"
        )

        for c in reversed(candles[:-1]):
            is_bearish = (
                c["close"] < c["open"]
            )

            if (
                want_bearish_candle
                and is_bearish
            ):
                return {
                    "direction": "up",
                    "high": c["high"],
                    "low": c["low"],
                }

            if (
                not want_bearish_candle
                and not is_bearish
            ):
                return {
                    "direction": "down",
                    "high": c["high"],
                    "low": c["low"],
                }

        return None

    def _find_fvg(self, direction):
        candles = list(self.candles)

        if len(candles) < 3:
            return None

        c1 = candles[-3]
        c2 = candles[-2]
        c3 = candles[-1]

        if (
            direction == "up"
            and c1["high"] < c3["low"]
        ):
            return {
                "bottom": c1["high"],
                "top": c3["low"],
            }

        if (
            direction == "down"
            and c1["low"] > c3["high"]
        ):
            return {
                "bottom": c3["high"],
                "top": c1["low"],
            }

        return None

    def _price_in_ob(self, candle):
        if not self.pending_ob:
            return False

        ob = self.pending_ob

        return (
            candle["low"] <= ob["high"]
            and candle["high"] >= ob["low"]
        )
