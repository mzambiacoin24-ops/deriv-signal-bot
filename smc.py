from collections import deque


class SMCAnalyzer:
    def __init__(self, symbol, lookback=2, history=300):
        self.symbol = symbol
        self.lookback = lookback
        self.candles = deque(maxlen=history)

        # Confirmed market direction
        self.trend = None

        # Confirmed swings
        self.last_swing_high = None
        self.last_swing_low = None

        self.swing_highs = deque(maxlen=15)
        self.swing_lows = deque(maxlen=15)

        # Current setup
        self.pending_ob = None
        self.pending_fvg = None

        # Setup age
        self.setup_age = None
        self.setup_max_age = 12

        # Market events
        self.last_event = None

        # Liquidity sweep
        self.last_sweep = None
        self.sweep_age = None

        # Chronological structure
        self.structure = deque(maxlen=8)

        # Structure points
        self.last_structure_high = None
        self.previous_structure_high = None

        self.last_structure_low = None
        self.previous_structure_low = None

    def add_candle(self, candle):
        self.candles.append(candle)

        # --------------------------------------------------
        # AGE LIQUIDITY SWEEP
        # --------------------------------------------------

        if self.sweep_age is not None:
            self.sweep_age += 1

            if self.sweep_age > 6:
                self.last_sweep = None
                self.sweep_age = None

        # --------------------------------------------------
        # AGE PENDING SETUP
        # --------------------------------------------------

        if self.setup_age is not None:
            self.setup_age += 1

            if self.setup_age > self.setup_max_age:
                self.pending_ob = None
                self.pending_fvg = None
                self.setup_age = None

        # --------------------------------------------------
        # CHECK OB RETEST
        # --------------------------------------------------

        entry_signal = None

        if self.pending_ob:
            if self._price_in_ob(candle):

                entry_signal = {
                    "direction": self.pending_ob["direction"],
                    "ob": dict(self.pending_ob),
                    "fvg": (
                        dict(self.pending_fvg)
                        if self.pending_fvg
                        else None
                    ),
                }

                # Setup consumed
                self.pending_ob = None
                self.pending_fvg = None
                self.setup_age = None

                self.last_event = "OB_RETEST"

        # --------------------------------------------------
        # MARKET EVENTS
        # --------------------------------------------------

        self._detect_liquidity_sweep(candle)

        self._detect_confirmed_swing()

        return entry_signal

    # ======================================================
    # LIQUIDITY SWEEP
    # ======================================================

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

    # ======================================================
    # CONFIRMED SWING
    # ======================================================

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

    # ======================================================
    # REGISTER HIGH
    # ======================================================

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

    # ======================================================
    # REGISTER LOW
    # ======================================================

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

    # ======================================================
    # STRUCTURE POINT
    # ======================================================

    def _add_structure_point(
        self,
        swing_type,
        price,
    ):

        if self.structure:

            last_type, last_price = (
                self.structure[-1]
            )

            # If same type appears again,
            # keep the more recent extreme.
            if last_type == swing_type:

                if swing_type == "H":

                    if price > last_price:
                        self.structure[-1] = (
                            swing_type,
                            price,
                        )

                else:

                    if price < last_price:
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

    # ======================================================
    # MARKET STRUCTURE
    # ======================================================

    def _evaluate_market_structure(self):

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
        # BEARISH
        #
        # H -> L -> H -> L
        #
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

            if self.trend != "down":

                self._trigger_choch("down")

            else:

                self.trend = "down"
                self.last_event = "BOS_DOWN"

            return

        # --------------------------------------------------
        # BULLISH
        #
        # L -> H -> L -> H
        #
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

            if self.trend != "up":

                self._trigger_choch("up")

            else:

                self.trend = "up"
                self.last_event = "BOS_UP"

            return

        # --------------------------------------------------
        # INDIVIDUAL EVENTS
        # --------------------------------------------------

        if len(points) >= 2:

            prev_type, prev_price = (
                points[-2]
            )

            last_type, last_price = (
                points[-1]
            )

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

    # ======================================================
    # CHOCH
    # ======================================================

    def _trigger_choch(self, new_direction):

        self.trend = new_direction

        self.last_event = (
            "CHOCH_"
            + new_direction.upper()
        )

        # --------------------------------------------------
        # IMPORTANT CHANGE
        #
        # We no longer require FVG and OB to be created
        # on the exact CHOCH candle.
        #
        # Search recent candles instead.
        # --------------------------------------------------

        self.pending_ob = (
            self._find_recent_order_block(
                new_direction
            )
        )

        self.pending_fvg = (
            self._find_recent_fvg(
                new_direction
            )
        )

        # If no OB was found immediately,
        # keep looking for one in the next candles.
        self.setup_age = 0

    # ======================================================
    # FIND RECENT ORDER BLOCK
    # ======================================================

    def _find_recent_order_block(
        self,
        direction,
    ):

        candles = list(self.candles)

        if len(candles) < 2:
            return None

        # Search up to 8 candles backwards.
        recent = candles[-9:-1]

        want_bearish = (
            direction == "up"
        )

        # Search newest first
        for c in reversed(recent):

            is_bearish = (
                c["close"] < c["open"]
            )

            # Bullish setup:
            # last bearish candle before displacement
            if (
                want_bearish
                and is_bearish
            ):

                return {
                    "direction": "up",
                    "high": c["high"],
                    "low": c["low"],
                }

            # Bearish setup:
            # last bullish candle before displacement
            if (
                not want_bearish
                and not is_bearish
            ):

                return {
                    "direction": "down",
                    "high": c["high"],
                    "low": c["low"],
                }

        return None

    # ======================================================
    # FIND RECENT FVG
    # ======================================================

    def _find_recent_fvg(
        self,
        direction,
    ):

        candles = list(self.candles)

        if len(candles) < 3:
            return None

        # Search several recent 3-candle combinations.
        start = max(
            0,
            len(candles) - 10,
        )

        for i in range(
            len(candles) - 3,
            start - 1,
            -1,
        ):

            c1 = candles[i]
            c2 = candles[i + 1]
            c3 = candles[i + 2]

            # --------------------------------------------------
            # BULLISH FVG
            #
            # Candle 1 high < Candle 3 low
            # --------------------------------------------------

            if (
                direction == "up"
                and c1["high"] < c3["low"]
            ):

                return {
                    "bottom": c1["high"],
                    "top": c3["low"],
                }

            # --------------------------------------------------
            # BEARISH FVG
            #
            # Candle 1 low > Candle 3 high
            # --------------------------------------------------

            if (
                direction == "down"
                and c1["low"] > c3["high"]
            ):

                return {
                    "bottom": c3["high"],
                    "top": c1["low"],
                }

        return None

    # ======================================================
    # CHECK PRICE INSIDE ORDER BLOCK
    # ======================================================

    def _price_in_ob(self, candle):

        if not self.pending_ob:
            return False

        ob = self.pending_ob

        return (
            candle["low"] <= ob["high"]
            and
            candle["high"] >= ob["low"]
        )

    # ======================================================
    # CONTINUE LOOKING FOR MISSING OB/FVG
    # ======================================================

    def _refresh_pending_setup(self):

        if self.setup_age is None:
            return

        # If OB is missing, search recent candles.
        if self.pending_ob is None:

            self.pending_ob = (
                self._find_recent_order_block(
                    self.trend
                )
            )

        # If FVG is missing, search recent candles.
        if self.pending_fvg is None:

            self.pending_fvg = (
                self._find_recent_fvg(
                    self.trend
                )
        )
