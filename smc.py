from collections import deque


class SMCAnalyzer:
    def __init__(self, symbol, lookback=2, history=300):
        self.symbol = symbol
        self.lookback = lookback
        self.candles = deque(maxlen=history)

        # Confirmed market direction
        self.trend = None

        # Latest confirmed swings
        self.last_swing_high = None
        self.last_swing_low = None

        self.swing_highs = deque(maxlen=15)
        self.swing_lows = deque(maxlen=15)

        # Pending setup
        self.pending_ob = None
        self.pending_fvg = None

        self.last_event = None

        # Liquidity sweep
        self.last_sweep = None
        self.sweep_age = None

        # Chronological market structure
        self.structure = deque(maxlen=8)

        self.last_structure_high = None
        self.previous_structure_high = None

        self.last_structure_low = None
        self.previous_structure_low = None

    def add_candle(self, candle):
        self.candles.append(candle)

        # Age liquidity sweep
        if self.sweep_age is not None:
            self.sweep_age += 1

            if self.sweep_age > 6:
                self.last_sweep = None
                self.sweep_age = None

        entry_signal = None

        # Wait for OB retest
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

        # Detect liquidity sweep
        self._detect_liquidity_sweep(candle)

        # Detect confirmed swing
        self._detect_confirmed_swing()

        return entry_signal

    def _detect_liquidity_sweep(self, candle):
        # Sweep previous high
        if self.last_swing_high is not None:
            if (
                candle["high"] > self.last_swing_high
                and candle["close"] < self.last_swing_high
            ):
                self.last_sweep = "high"
                self.sweep_age = 0
                self.last_event = "SWEEP_HIGH"

        # Sweep previous low
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

        self.previous_structure_high = (
            self.last_structure_high
        )

        self.last_structure_high = high

        self.last_swing_high = high
        self.swing_highs.append(high)

        self._add_structure_point(
            "H",
            high,
        )

        self._evaluate_market_structure()

    def _register_swing_low(self, candle):
        low = candle["low"]

        self.previous_structure_low = (
            self.last_structure_low
        )

        self.last_structure_low = low

        self.last_swing_low = low
        self.swing_lows.append(low)

        self._add_structure_point(
            "L",
            low,
        )

        self._evaluate_market_structure()

    def _add_structure_point(self, swing_type, price):
        """
        Keep the structure chronological.

        If two highs appear before a low, keep only
        the newest high. Same for two lows.

        This gives us a cleaner sequence such as:

        H -> L -> H -> L

        or

        L -> H -> L -> H
        """

        if self.structure:
            last_type, last_price = self.structure[-1]

            if last_type == swing_type:
                # Replace the previous swing of the same type
                # with the newer confirmed swing.
                self.structure[-1] = (
                    swing_type,
                    price,
                )
                return

        self.structure.append(
            (
                swing_type,
                price,
            )
        )

    def _evaluate_market_structure(self):
        """
        Confirm market structure using chronological
        swing sequence.

        Bearish confirmation:

            H1 -> L1 -> H2 -> L2

            H2 < H1
            L2 < L1

        This gives:

            Lower High
            Lower Low

        Bullish confirmation:

            L1 -> H1 -> L2 -> H2

            L2 > L1
            H2 > H1

        This gives:

            Higher Low
            Higher High
        """

        if len(self.structure) < 4:
            return

        points = list(self.structure)

        p1 = points[-4]
        p2 = points[-3]
        p3 = points[-2]
        p4 = points[-1]

        t1, v1 = p1
        t2, v2 = p2
        t3, v3 = p3
        t4, v4 = p4

        # --------------------------------------------------
        # BEARISH STRUCTURE
        #
        # H -> L -> H -> L
        # H2 < H1
        # L2 < L1
        # --------------------------------------------------

        bearish_structure = (
            t1 == "H"
            and t2 == "L"
            and t3 == "H"
            and t4 == "L"
            and v3 < v1
            and v4 < v2
        )

        if bearish_structure:
            if self.trend == "up":
                self._trigger_choch("down")
            else:
                self.trend = "down"
                self.last_event = "BOS_DOWN"

            return

        # --------------------------------------------------
        # BULLISH STRUCTURE
        #
        # L -> H -> L -> H
        # L2 > L1
        # H2 > H1
        # --------------------------------------------------

        bullish_structure = (
            t1 == "L"
            and t2 == "H"
            and t3 == "L"
            and t4 == "H"
            and v3 > v1
            and v4 > v2
        )

        if bullish_structure:
            if self.trend == "down":
                self._trigger_choch("up")
            else:
                self.trend = "up"
                self.last_event = "BOS_UP"

            return

        # --------------------------------------------------
        # Individual structure events
        # --------------------------------------------------

        if len(points) >= 2:
            prev_type, prev_price = points[-2]
            last_type, last_price = points[-1]

            if (
                prev_type == "H"
                and last_type == "H"
            ):
                if last_price > prev_price:
                    self.last_event = "HH"

                elif last_price < prev_price:
                    self.last_event = "LH"

            elif (
                prev_type == "L"
                and last_type == "L"
            ):
                if last_price > prev_price:
                    self.last_event = "HL"

                elif last_price < prev_price:
                    self.last_event = "LL"

    def _trigger_choch(self, new_direction):
        self.trend = new_direction

        self.last_event = (
            "CHOCH_"
            + new_direction.upper()
        )

        # Create new setup only after confirmed
        # market structure change.
        self.pending_ob = (
            self._find_order_block(
                new_direction
            )
        )

        self.pending_fvg = (
            self._find_fvg(
                new_direction
            )
        )

    def _find_order_block(self, direction):
        candles = list(self.candles)

        # For bullish setup we want the last bearish candle.
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

        # Bullish FVG
        if (
            direction == "up"
            and c1["high"] < c3["low"]
        ):
            return {
                "bottom": c1["high"],
                "top": c3["low"],
            }

        # Bearish FVG
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
