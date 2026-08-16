from collections import deque


class SMCAnalyzer:
    """
    SMC / Market Structure analyzer.

    Inalenga kutenganisha:
        1. Market bias
        2. Current structure
        3. Liquidity sweep
        4. Displacement
        5. BOS / CHOCH
        6. Order Block / FVG
        7. Pullback
        8. Entry timing

    Muhimu:
    - Candle ile ile ikituma ticks nyingi inabadilishwa, haiongezwi mara nyingi.
    - Setup ya zamani haiwezi kubaki milele.
    - Direction pekee haitoshi kutoa entry.
    """

    def __init__(
        self,
        symbol,
        lookback=2,
        history=300,
    ):
        self.symbol = symbol
        self.lookback = lookback

        self.candles = deque(maxlen=history)

        self.swing_highs = deque(maxlen=30)
        self.swing_lows = deque(maxlen=30)

        self.last_swing_high = None
        self.last_swing_low = None

        self.previous_swing_high = None
        self.previous_swing_low = None

        self.trend = None
        self.structure_strength = "NEUTRAL"

        self.last_event = None

        self.last_sweep = None
        self.sweep_age = None
        self.last_sweep_epoch = None

        self.pending_ob = None
        self.pending_fvg = None

        self.pending_direction = None
        self.pending_epoch = None
        self.pending_age = 0

        self.last_setup = None
        self.last_setup_epoch = None

        self.last_break_epoch = None

        self.bullish_score = 0
        self.bearish_score = 0

    # =========================================================
    # ADD / UPDATE CANDLE
    # =========================================================

    def add_candle(self, candle):

        required = (
            "open",
            "high",
            "low",
            "close",
        )

        for key in required:
            if key not in candle:
                raise ValueError(
                    "Candle must contain open, high, low and close"
                )

        c = {
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "epoch": candle.get("epoch"),
        }

        epoch = c.get("epoch")

        # -----------------------------------------------------
        # LIVE CANDLE UPDATE
        # -----------------------------------------------------

        if self.candles and epoch is not None:

            last_epoch = self.candles[-1].get("epoch")

            if last_epoch == epoch:
                self.candles[-1] = c

                # Live candle inaweza kubadilisha sweep,
                # lakini haitengenezi setup mpya mara nyingi.
                self._detect_liquidity_sweep(c)
                self._update_structure()

                return self._check_pullback_entry(c)

        # -----------------------------------------------------
        # NEW CANDLE
        # -----------------------------------------------------

        if self.sweep_age is not None:
            self.sweep_age += 1

            if self.sweep_age > 6:
                self.last_sweep = None
                self.last_sweep_epoch = None
                self.sweep_age = None

        if self.pending_direction is not None:
            self.pending_age += 1

            if self.pending_age > 10:
                self._clear_pending_setup()

        self.candles.append(c)

        # Swing lazima ithibitishwe kwanza.
        self._detect_confirmed_swing()

        # Structure ya sasa.
        self._update_structure()

        # Sweep ya sasa.
        self._detect_liquidity_sweep(c)

        # Break ya structure.
        self._detect_structure_break()

        # Structure inaweza kubadilika baada ya break.
        self._update_structure()

        # Entry timing.
        return self._check_pullback_entry(c)

    # =========================================================
    # CONFIRMED SWINGS
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

        center = candles[center_index]

        left = candles[
            center_index - self.lookback:
            center_index
        ]

        right = candles[
            center_index + 1:
            center_index + 1 + self.lookback
        ]

        if len(right) < self.lookback:
            return

        is_high = (
            all(
                center["high"] > x["high"]
                for x in left
            )
            and
            all(
                center["high"] > x["high"]
                for x in right
            )
        )

        is_low = (
            all(
                center["low"] < x["low"]
                for x in left
            )
            and
            all(
                center["low"] < x["low"]
                for x in right
            )
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
            and price == self.swing_highs[-1]
        ):
            return

        self.previous_swing_high = (
            self.last_swing_high
        )

        self.last_swing_high = float(price)

        self.swing_highs.append(
            float(price)
        )

    def _register_swing_low(self, price):

        if (
            self.swing_lows
            and price == self.swing_lows[-1]
        ):
            return

        self.previous_swing_low = (
            self.last_swing_low
        )

        self.last_swing_low = float(price)

        self.swing_lows.append(
            float(price)
        )

    # =========================================================
    # MARKET STRUCTURE
    # =========================================================

    def _update_structure(self):

        highs = list(self.swing_highs)
        lows = list(self.swing_lows)

        self.bullish_score = 0
        self.bearish_score = 0

        # -----------------------------------------------------
        # SWING STRUCTURE
        # -----------------------------------------------------

        if len(highs) >= 2:

            old_high = highs[-2]
            new_high = highs[-1]

            if new_high > old_high:
                self.bullish_score += 2

            elif new_high < old_high:
                self.bearish_score += 2

        if len(lows) >= 2:

            old_low = lows[-2]
            new_low = lows[-1]

            if new_low > old_low:
                self.bullish_score += 2

            elif new_low < old_low:
                self.bearish_score += 2

        # -----------------------------------------------------
        # RECENT PRESSURE
        # -----------------------------------------------------

        candles = list(self.candles)

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
        # STRUCTURE DIRECTION
        #
        # Hakuna midpoint tena kama sababu ya msingi ya
        # kuamua direction.
        # -----------------------------------------------------

        if (
            self.bullish_score >= 4
            and self.bullish_score
            >= self.bearish_score + 2
        ):

            self.trend = "up"

            self.structure_strength = (
                "STRONG"
                if self.bullish_score >= 5
                else "MODERATE"
            )

        elif (
            self.bearish_score >= 4
            and self.bearish_score
            >= self.bullish_score + 2
        ):

            self.trend = "down"

            self.structure_strength = (
                "STRONG"
                if self.bearish_score >= 5
                else "MODERATE"
            )

        else:

            self.structure_strength = "NEUTRAL"

    # =========================================================
    # STRUCTURE BREAK
    # =========================================================

    def _detect_structure_break(self):

        if len(self.candles) < 5:
            return None

        candle = self.candles[-1]
        epoch = candle.get("epoch")

        # -----------------------------------------------------
        # BREAK UP
        # -----------------------------------------------------

        if (
            self.last_swing_high is not None
            and candle["close"]
            > self.last_swing_high
        ):

            if epoch == self.last_break_epoch:
                return None

            old_trend = self.trend

            if old_trend == "down":
                event = "CHOCH_UP"
            else:
                event = "BOS_UP"

            # Displacement lazima iwepo.
            if not self._has_displacement(
                direction="up"
            ):
                return None

            self.trend = "up"
            self.last_event = event
            self.last_break_epoch = epoch

            self.pending_direction = "up"
            self.pending_epoch = epoch
            self.pending_age = 0

            self.pending_ob = (
                self._find_order_block("up")
            )

            self.pending_fvg = (
                self._find_fvg("up")
            )

            return event

        # -----------------------------------------------------
        # BREAK DOWN
        # -----------------------------------------------------

        if (
            self.last_swing_low is not None
            and candle["close"]
            < self.last_swing_low
        ):

            if epoch == self.last_break_epoch:
                return None

            old_trend = self.trend

            if old_trend == "up":
                event = "CHOCH_DOWN"
            else:
                event = "BOS_DOWN"

            if not self._has_displacement(
                direction="down"
            ):
                return None

            self.trend = "down"
            self.last_event = event
            self.last_break_epoch = epoch

            self.pending_direction = "down"
            self.pending_epoch = epoch
            self.pending_age = 0

            self.pending_ob = (
                self._find_order_block("down")
            )

            self.pending_fvg = (
                self._find_fvg("down")
            )

            return event

        return None

    # =========================================================
    # DISPLACEMENT
    # =========================================================

    def _has_displacement(
        self,
        direction,
    ):

        candles = list(self.candles)

        if len(candles) < 6:
            return False

        c = candles[-1]

        candle_range = (
            c["high"] - c["low"]
        )

        if candle_range <= 0:
            return False

        body = abs(
            c["close"] - c["open"]
        )

        body_ratio = (
            body / candle_range
        )

        previous = candles[-6:-1]

        ranges = [
            x["high"] - x["low"]
            for x in previous
            if x["high"] > x["low"]
        ]

        if not ranges:
            return False

        average_range = (
            sum(ranges) / len(ranges)
        )

        range_expansion = (
            candle_range
            >= average_range * 1.05
        )

        if direction == "up":

            directional_body = (
                c["close"] > c["open"]
            )

        else:

            directional_body = (
                c["close"] < c["open"]
            )

        return (
            directional_body
            and body_ratio >= 0.50
            and range_expansion
        )

    # =========================================================
    # LIQUIDITY SWEEP
    # =========================================================

    def _detect_liquidity_sweep(
        self,
        candle,
    ):

        sweep = None

        if (
            self.last_swing_high is not None
            and candle["high"]
            > self.last_swing_high
            and candle["close"]
            < self.last_swing_high
        ):

            sweep = "high"

        elif (
            self.last_swing_low is not None
            and candle["low"]
            < self.last_swing_low
            and candle["close"]
            > self.last_swing_low
        ):

            sweep = "low"

        if sweep is not None:

            self.last_sweep = sweep
            self.sweep_age = 0
            self.last_sweep_epoch = (
                candle.get("epoch")
            )

            self.last_event = (
                "SWEEP_HIGH"
                if sweep == "high"
                else "SWEEP_LOW"
            )

    # =========================================================
    # ORDER BLOCK
    # =========================================================

    def _find_order_block(
        self,
        direction,
    ):

        candles = list(self.candles)

        if len(candles) < 4:
            return None

        # Search only candles immediately preceding
        # the displacement.
        search = candles[:-1][-8:]

        for c in reversed(search):

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

    def _find_fvg(
        self,
        direction,
    ):

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
                "direction": "up",
                "bottom": c1["high"],
                "top": c3["low"],
                "epoch": c3.get("epoch"),
            }

        if (
            direction == "down"
            and c1["low"] > c3["high"]
        ):

            return {
                "direction": "down",
                "bottom": c3["high"],
                "top": c1["low"],
                "epoch": c3.get("epoch"),
            }

        return None

    # =========================================================
    # PULLBACK / ENTRY TIMING
    # =========================================================

    def _check_pullback_entry(
        self,
        candle,
    ):

        # -----------------------------------------------------
        # Direction lazima iwepo.
        # -----------------------------------------------------

        if self.trend not in (
            "up",
            "down",
        ):
            return None

        # -----------------------------------------------------
        # Setup lazima iwe na BREAK mpya.
        #
        # Hii ndiyo tofauti kubwa na logic ya zamani.
        # Direction peke yake haiwezi kuanzisha entry.
        # -----------------------------------------------------

        if (
            self.pending_direction is None
            or self.pending_epoch is None
        ):
            return None

        if (
            self.pending_direction
            != self.trend
        ):
            return None

        # -----------------------------------------------------
        # Break isiwe ya zamani sana.
        # -----------------------------------------------------

        if self.pending_age > 10:
            self._clear_pending_setup()
            return None

        # -----------------------------------------------------
        # Candle ya break yenyewe haitumiki kama pullback entry.
        # -----------------------------------------------------

        if (
            candle.get("epoch")
            == self.pending_epoch
        ):
            return None

        # -----------------------------------------------------
        # Candle lazima iwe na movement halisi.
        # -----------------------------------------------------

        candle_range = (
            candle["high"]
            - candle["low"]
        )

        if candle_range <= 0:
            return None

        body = abs(
            candle["close"]
            - candle["open"]
        )

        body_ratio = (
            body / candle_range
        )

        if body_ratio < 0.25:
            return None

        price = candle["close"]

        # =====================================================
        # BUY
        # =====================================================

        if self.pending_direction == "up":

            # Lazima kuwe na bullish reaction.
            bullish_candle = (
                candle["close"]
                > candle["open"]
            )

            if not bullish_candle:
                return None

            # Price lazima iwe imerudi kwenye setup zone.
            touched_ob = (
                self._price_near_zone(
                    candle,
                    self.pending_ob,
                )
            )

            touched_fvg = (
                self._price_near_zone(
                    candle,
                    self.pending_fvg,
                )
            )

            # Fallback ndogo:
            # karibu na swing low mpya, lakini sio
            # karibu na swing high.
            near_low = (
                self._near_recent_low(
                    price,
                    tolerance=0.0018,
                )
            )

            near_zone = (
                touched_ob
                or touched_fvg
                or near_low
            )

            if not near_zone:
                return None

            # Usinunue kama candle yenyewe imekwenda
            # moja kwa moja karibu na recent high.
            if self._too_close_to_recent_high(
                price
            ):
                return None

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
                "sweep_epoch": self.last_sweep_epoch,
                "break_epoch": self.pending_epoch,
                "score": self.bullish_score,
                "timing": "CONFIRMED",
            }

            self.last_setup = setup
            self.last_setup_epoch = (
                candle.get("epoch")
            )

            self._clear_pending_setup()

            return setup

        # =====================================================
        # SELL
        # =====================================================

        if self.pending_direction == "down":

            bearish_candle = (
                candle["close"]
                < candle["open"]
            )

            if not bearish_candle:
                return None

            touched_ob = (
                self._price_near_zone(
                    candle,
                    self.pending_ob,
                )
            )

            touched_fvg = (
                self._price_near_zone(
                    candle,
                    self.pending_fvg,
                )
            )

            near_high = (
                self._near_recent_high(
                    price,
                    tolerance=0.0018,
                )
            )

            near_zone = (
                touched_ob
                or touched_fvg
                or near_high
            )

            if not near_zone:
                return None

            if self._too_close_to_recent_low(
                price
            ):
                return None

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
                "sweep_epoch": self.last_sweep_epoch,
                "break_epoch": self.pending_epoch,
                "score": self.bearish_score,
                "timing": "CONFIRMED",
            }

            self.last_setup = setup
            self.last_setup_epoch = (
                candle.get("epoch")
            )

            self._clear_pending_setup()

            return setup

        return None

    # =========================================================
    # CLEAR PENDING SETUP
    # =========================================================

    def _clear_pending_setup(self):

        self.pending_ob = None
        self.pending_fvg = None
        self.pending_direction = None
        self.pending_epoch = None
        self.pending_age = 0

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

        if "high" in zone:
            high = float(zone["high"])
            low = float(zone["low"])

        else:
            high = float(zone["top"])
            low = float(zone["bottom"])

        return (
            candle["low"] <= high
            and candle["high"] >= low
        )

    def _near_recent_low(
        self,
        price,
        tolerance=0.0018,
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
                <= tolerance
            ):
                return True

        return False

    def _near_recent_high(
        self,
        price,
        tolerance=0.0018,
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
                <= tolerance
            ):
                return True

        return False

    def _too_close_to_recent_high(
        self,
        price,
    ):

        if not self.swing_highs:
            return False

        recent = list(
            self.swing_highs
        )[-2:]

        if not recent:
            return False

        highest = max(recent)

        distance = abs(
            highest - price
        )

        reference = max(
            abs(highest),
            0.0000001,
        )

        return (
            distance / reference
            <= 0.001
        )

    def _too_close_to_recent_low(
        self,
        price,
    ):

        if not self.swing_lows:
            return False

        recent = list(
            self.swing_lows
        )[-2:]

        if not recent:
            return False

        lowest = min(recent)

        distance = abs(
            price - lowest
        )

        reference = max(
            abs(lowest),
            0.0000001,
        )

        return (
            distance / reference
            <= 0.001
        )

    # =========================================================
    # PUBLIC STRUCTURE
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
            "last_sweep_epoch": self.last_sweep_epoch,
            "last_swing_high": self.last_swing_high,
            "last_swing_low": self.last_swing_low,
            "pending_direction": self.pending_direction,
            "pending_epoch": self.pending_epoch,
            "pending_age": self.pending_age,
        }

    def get_levels(self):

        return {
            "swing_highs": list(
                self.swing_highs
            ),
            "swing_lows": list(
                self.swing_lows
            ),
            "pending_ob": (
                dict(self.pending_ob)
                if self.pending_ob
                else None
            ),
            "pending_fvg": (
                dict(self.pending_fvg)
                if self.pending_fvg
                else None
            ),
        }
