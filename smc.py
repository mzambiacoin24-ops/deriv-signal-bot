from collections import deque


class SMCAnalyzer:
    """
    SMC/Market Structure engine mpya.

    Logic:
    - Hutambua HH/HL na LH/LL
    - Hutumia structure, si swing moja tu, kuamua direction
    - Hutambua BOS na CHOCH
    - Hutambua liquidity sweep
    - Hutambua FVG
    - Hutambua Order Block
    - Hutoa setup ya pullback badala ya kusubiri filters zote
    """

    def __init__(
        self,
        symbol,
        lookback=2,
        history=300,
    ):
        self.symbol = symbol
        self.lookback = lookback

        self.candles = deque(
            maxlen=history
        )

        self.swing_highs = deque(
            maxlen=30
        )

        self.swing_lows = deque(
            maxlen=30
        )

        self.last_swing_high = None
        self.last_swing_low = None

        self.previous_swing_high = None
        self.previous_swing_low = None

        self.trend = None
        self.structure_strength = "NEUTRAL"

        self.last_event = None
        self.last_sweep = None
        self.sweep_age = None

        self.pending_ob = None
        self.pending_fvg = None

        self.last_setup = None

        self.bullish_score = 0
        self.bearish_score = 0

    # =========================================================
    # ADD CANDLE
    # =========================================================

    def add_candle(self, candle):
        self.candles.append(dict(candle))

        if self.sweep_age is not None:
            self.sweep_age += 1

            if self.sweep_age > 8:
                self.last_sweep = None
                self.sweep_age = None

        self._detect_confirmed_swing()
        self._detect_liquidity_sweep(candle)

        self._update_structure()

        setup = self._check_pullback_entry(candle)

        return setup

    # =========================================================
    # STRUCTURE
    # =========================================================

    def _detect_confirmed_swing(self):
        n = len(self.candles)

        required = (
            self.lookback * 2
        ) + 1

        if n < required:
            return

        candles = list(self.candles)

        center_index = (
            n - 1 - self.lookback
        )

        if center_index < self.lookback:
            return

        center = candles[
            center_index
        ]

        left = candles[
            center_index
            - self.lookback:
            center_index
        ]

        right = candles[
            center_index + 1:
            center_index
            + 1
            + self.lookback
        ]

        if len(right) < self.lookback:
            return

        is_high = all(
            center["high"] > x["high"]
            for x in left
        ) and all(
            center["high"] > x["high"]
            for x in right
        )

        is_low = all(
            center["low"] < x["low"]
            for x in left
        ) and all(
            center["low"] < x["low"]
            for x in right
        )

        if is_high:
            self._register_swing_high(
                center["high"]
            )

        if is_low:
            self._register_swing_low(
                center["low"]
            )

    def _register_swing_high(self, price):
        if (
            self.swing_highs
            and price
            == self.swing_highs[-1]
        ):
            return

        self.previous_swing_high = (
            self.last_swing_high
        )

        self.last_swing_high = price

        self.swing_highs.append(price)

    def _register_swing_low(self, price):
        if (
            self.swing_lows
            and price
            == self.swing_lows[-1]
        ):
            return

        self.previous_swing_low = (
            self.last_swing_low
        )

        self.last_swing_low = price

        self.swing_lows.append(price)

    # =========================================================
    # MARKET STRUCTURE ENGINE
    # =========================================================

    def _update_structure(self):
        highs = list(
            self.swing_highs
        )

        lows = list(
            self.swing_lows
        )

        self.bullish_score = 0
        self.bearish_score = 0

        # -----------------------------------------------------
        # Need at least two highs and two lows
        # -----------------------------------------------------

        if len(highs) >= 2:
            h1 = highs[-2]
            h2 = highs[-1]

            if h2 > h1:
                self.bullish_score += 2

            elif h2 < h1:
                self.bearish_score += 2

        if len(lows) >= 2:
            l1 = lows[-2]
            l2 = lows[-1]

            if l2 > l1:
                self.bullish_score += 2

            elif l2 < l1:
                self.bearish_score += 2

        # -----------------------------------------------------
        # Recent candle pressure
        # -----------------------------------------------------

        candles = list(
            self.candles
        )

        if len(candles) >= 6:
            recent = candles[-6:]

            bullish = 0
            bearish = 0

            for c in recent:
                if c["close"] > c["open"]:
                    bullish += 1
                elif c["close"] < c["open"]:
                    bearish += 1

            if bullish >= 4:
                self.bullish_score += 1

            if bearish >= 4:
                self.bearish_score += 1

        # -----------------------------------------------------
        # Price location relative to recent structure
        # -----------------------------------------------------

        if (
            self.last_swing_high is not None
            and self.last_swing_low is not None
            and candles
        ):
            price = candles[-1]["close"]

            midpoint = (
                self.last_swing_high
                + self.last_swing_low
            ) / 2

            if price > midpoint:
                self.bullish_score += 1

            elif price < midpoint:
                self.bearish_score += 1

        # -----------------------------------------------------
        # Final direction
        # -----------------------------------------------------

        if (
            self.bullish_score
            >= 4
            and self.bullish_score
            >= self.bearish_score + 2
        ):
            new_trend = "up"

            if self.trend != new_trend:
                self.last_event = "STRUCTURE_UP"

            self.trend = new_trend
            self.structure_strength = (
                "STRONG"
                if self.bullish_score >= 5
                else "MODERATE"
            )

        elif (
            self.bearish_score
            >= 4
            and self.bearish_score
            >= self.bullish_score + 2
        ):
            new_trend = "down"

            if self.trend != new_trend:
                self.last_event = "STRUCTURE_DOWN"

            self.trend = new_trend
            self.structure_strength = (
                "STRONG"
                if self.bearish_score >= 5
                else "MODERATE"
            )

        else:
            self.structure_strength = (
                "NEUTRAL"
            )

    # =========================================================
    # BOS / CHOCH
    # =========================================================

    def detect_structure_break(self):
        if len(self.candles) < 3:
            return None

        candle = self.candles[-1]

        event = None

        if (
            self.last_swing_high is not None
            and candle["close"]
            > self.last_swing_high
        ):
            if self.trend == "down":
                event = "CHOCH_UP"
            else:
                event = "BOS_UP"

            self.trend = "up"
            self.last_event = event

            self.pending_ob = (
                self._find_order_block("up")
            )

            self.pending_fvg = (
                self._find_fvg("up")
            )

        elif (
            self.last_swing_low is not None
            and candle["close"]
            < self.last_swing_low
        ):
            if self.trend == "up":
                event = "CHOCH_DOWN"
            else:
                event = "BOS_DOWN"

            self.trend = "down"
            self.last_event = event

            self.pending_ob = (
                self._find_order_block("down")
            )

            self.pending_fvg = (
                self._find_fvg("down")
            )

        return event

    # =========================================================
    # LIQUIDITY SWEEP
    # =========================================================

    def _detect_liquidity_sweep(self, candle):
        if (
            self.last_swing_high is not None
            and candle["high"]
            > self.last_swing_high
            and candle["close"]
            < self.last_swing_high
        ):
            self.last_sweep = "high"
            self.sweep_age = 0
            self.last_event = (
                "SWEEP_HIGH"
            )

        if (
            self.last_swing_low is not None
            and candle["low"]
            < self.last_swing_low
            and candle["close"]
            > self.last_swing_low
        ):
            self.last_sweep = "low"
            self.sweep_age = 0
            self.last_event = (
                "SWEEP_LOW"
            )

    # =========================================================
    # ORDER BLOCK
    # =========================================================

    def _find_order_block(self, direction):
        candles = list(
            self.candles
        )

        if len(candles) < 3:
            return None

        search = candles[
            :-1
        ]

        for c in reversed(search[-20:]):
            bullish = (
                c["close"] > c["open"]
            )

            bearish = (
                c["close"] < c["open"]
            )

            if (
                direction == "up"
                and bearish
            ):
                return {
                    "direction": "up",
                    "high": c["high"],
                    "low": c["low"],
                    "epoch": c.get("epoch"),
                }

            if (
                direction == "down"
                and bullish
            ):
                return {
                    "direction": "down",
                    "high": c["high"],
                    "low": c["low"],
                    "epoch": c.get("epoch"),
                }

        return None

    # =========================================================
    # FVG
    # =========================================================

    def _find_fvg(self, direction):
        candles = list(
            self.candles
        )

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
                "direction": "up",
                "bottom": c1["high"],
                "top": c3["low"],
            }

        if (
            direction == "down"
            and c1["low"] > c3["high"]
        ):
            return {
                "direction": "down",
                "bottom": c3["high"],
                "top": c1["low"],
            }

        return None

    # =========================================================
    # PULLBACK ENTRY
    # =========================================================

    def _check_pullback_entry(self, candle):
        """
        Entry haifanyiki kwa CHOCH ndogo pekee.

        Tunataka:
        1. Direction iwe confirmed.
        2. Price ifanye pullback.
        3. Pullback iguse OB au FVG
           au iwe karibu na recent structure.
        4. Candle ya mwisho ionyeshe rejection.
        """

        if self.trend not in (
            "up",
            "down",
        ):
            return None

        if (
            self.structure_strength
            == "NEUTRAL"
        ):
            return None

        candles = list(
            self.candles
        )

        if len(candles) < 8:
            return None

        price = candle["close"]

        # -----------------------------------------------------
        # BUY
        # -----------------------------------------------------

        if self.trend == "up":
            bullish_rejection = (
                candle["close"]
                > candle["open"]
            )

            near_zone = (
                self._price_near_zone(
                    candle,
                    self.pending_ob,
                )
                or
                self._price_near_zone(
                    candle,
                    self.pending_fvg,
                )
                or
                self._near_recent_low(
                    price
                )
            )

            if (
                bullish_rejection
                and near_zone
            ):
                setup = {
                    "direction": "up",
                    "reason": "BULLISH_PULLBACK",
                    "structure": self.trend,
                    "strength": self.structure_strength,
                    "ob": (
                        dict(self.pending_ob)
                        if self.pending_ob
                        else None
                    ),
                    "fvg": (
                        dict(self.pending_fvg)
                        if self.pending_fvg
                        else None
                    ),
                    "sweep": self.last_sweep,
                    "score": self.bullish_score,
                }

                self.last_setup = setup

                self.pending_ob = None
                self.pending_fvg = None

                return setup

        # -----------------------------------------------------
        # SELL
        # -----------------------------------------------------

        if self.trend == "down":
            bearish_rejection = (
                candle["close"]
                < candle["open"]
            )

            near_zone = (
                self._price_near_zone(
                    candle,
                    self.pending_ob,
                )
                or
                self._price_near_zone(
                    candle,
                    self.pending_fvg,
                )
                or
                self._near_recent_high(
                    price
                )
            )

            if (
                bearish_rejection
                and near_zone
            ):
                setup = {
                    "direction": "down",
                    "reason": "BEARISH_PULLBACK",
                    "structure": self.trend,
                    "strength": self.structure_strength,
                    "ob": (
                        dict(self.pending_ob)
                        if self.pending_ob
                        else None
                    ),
                    "fvg": (
                        dict(self.pending_fvg)
                        if self.pending_fvg
                        else None
                    ),
                    "sweep": self.last_sweep,
                    "score": self.bearish_score,
                }

                self.last_setup = setup

                self.pending_ob = None
                self.pending_fvg = None

                return setup

        return None

    # =========================================================
    # ZONE HELPERS
    # =========================================================

    def _price_near_zone(
        self,
        candle,
        zone,
    ):
        if not zone:
            return False

        high = float(
            zone["high"]
            if "high" in zone
            else zone["top"]
        )

        low = float(
            zone["low"]
            if "low" in zone
            else zone["bottom"]
        )

        return (
            candle["low"] <= high
            and candle["high"] >= low
        )

    def _near_recent_low(
        self,
        price,
    ):
        if not self.swing_lows:
            return False

        recent = list(
            self.swing_lows
        )[-3:]

        for level in recent:
            distance = abs(
                price - level
            )

            reference = max(
                abs(level),
                0.0000001,
            )

            if (
                distance / reference
                <= 0.003
            ):
                return True

        return False

    def _near_recent_high(
        self,
        price,
    ):
        if not self.swing_highs:
            return False

        recent = list(
            self.swing_highs
        )[-3:]

        for level in recent:
            distance = abs(
                price - level
            )

            reference = max(
                abs(level),
                0.0000001,
            )

            if (
                distance / reference
                <= 0.003
            ):
                return True

        return False

    # =========================================================
    # PUBLIC INFORMATION
    # =========================================================

    def get_structure(self):
        return {
            "symbol": self.symbol,
            "trend": self.trend,
            "strength": self.structure_strength,
            "bullish_score": self.bullish_score,
            "bearish_score": self.bearish_score,
            "last_event": self.last_event,
            "last_sweep": self.last_sweep,
            "last_swing_high": (
                self.last_swing_high
            ),
            "last_swing_low": (
                self.last_swing_low
            ),
        }

    def get_levels(self):
        return {
            "swing_highs": list(
                self.swing_highs
            ),
            "swing_lows": list(
                self.swing_lows
            ),
                }
