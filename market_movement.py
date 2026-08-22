from collections import deque


class MovementEngine:
    """
    Live movement engine for Deriv Volatility Indices.

    Volatility-specific principles:
    - Previous highs/lows are reaction zones.
    - A break is NOT automatically a trend continuation.
    - A break followed by rejection is treated as a possible reversal.
    - Live pressure and acceleration are used as confirmation.
    - Extreme candles are not chased.
    """

    def __init__(self, max_ticks=600, max_candles=80):
        self.ticks = deque(maxlen=max_ticks)
        self.candles = deque(maxlen=max_candles)

        self.last_epoch = None

        self.last_liquidity_level = None
        self.liquidity_event = None

        self.reversal_direction = None
        self.reversal_epoch = None
        self.reversal_age = 0

        self.breakout_direction = None
        self.breakout_level = None
        self.breakout_epoch = None
        self.breakout_age = 0

    def update_tick(self, price, epoch, candle):
        price = float(price)
        epoch = int(epoch)

        self.ticks.append((epoch, price))
        self.last_epoch = epoch

        if candle is not None:
            c = dict(candle)

            if (
                self.candles
                and self.candles[-1].get("epoch")
                == c.get("epoch")
            ):
                self.candles[-1] = c
            else:
                self.candles.append(c)

        self._detect_liquidity_event()

        return self.snapshot(candle)

    # =========================================================
    # RANGE / PRESSURE
    # =========================================================

    def _avg_range(self, count=20):
        values = [
            float(c["high"]) - float(c["low"])
            for c in list(self.candles)[-count:]
            if float(c["high"]) > float(c["low"])
        ]

        return (
            sum(values) / len(values)
            if values
            else 0.0
        )

    def _pressure(self, count=12):
        candles = list(
            self.candles
        )[-count:]

        up = 0.0
        down = 0.0

        for c in candles:
            rng = max(
                float(c["high"]) - float(c["low"]),
                1e-12,
            )

            body = (
                float(c["close"])
                - float(c["open"])
            )

            weight = min(
                abs(body) / rng,
                1.0,
            )

            if body > 0:
                up += weight
            elif body < 0:
                down += weight

        total = up + down

        return (
            (up - down) / total
            if total
            else 0.0
        )

    # =========================================================
    # REACTION LEVELS
    # =========================================================

    def _levels(self):
        candles = list(
            self.candles
        )

        if len(candles) < 6:
            return None, None

        previous = (
            candles[-21:-1]
            or candles[:-1]
        )

        if not previous:
            return None, None

        return (
            max(
                float(c["high"])
                for c in previous
            ),
            min(
                float(c["low"])
                for c in previous
            ),
        )

    # =========================================================
    # LIQUIDITY / FAILED BREAKOUT
    # =========================================================

    def _detect_liquidity_event(self):
        candles = list(
            self.candles
        )

        if len(candles) < 6:
            return

        c = candles[-1]

        high_level, low_level = (
            self._levels()
        )

        avg = self._avg_range(20)

        tolerance = (
            avg * 0.08
            if avg
            else 0.0
        )

        # Age breakout state.
        if self.breakout_direction:
            self.breakout_age += 1

            if self.breakout_age > 4:
                self.breakout_direction = None
                self.breakout_level = None
                self.breakout_epoch = None
                self.breakout_age = 0

        # Age reversal state.
        if self.reversal_direction:
            self.reversal_age += 1

            if self.reversal_age > 6:
                self.reversal_direction = None
                self.reversal_epoch = None
                self.reversal_age = 0

        event = None
        level = None

        # -----------------------------------------------------
        # Failed break ABOVE previous high
        # -----------------------------------------------------
        if (
            self.breakout_direction == "up"
            and self.breakout_level is not None
            and c.get("epoch")
            != self.breakout_epoch
            and c["close"]
            < self.breakout_level
        ):
            event = "FAILED_BREAKOUT_HIGH"
            level = self.breakout_level

            self.reversal_direction = "down"
            self.reversal_epoch = c.get(
                "epoch"
            )
            self.reversal_age = 0

        # -----------------------------------------------------
        # Failed break BELOW previous low
        # -----------------------------------------------------
        elif (
            self.breakout_direction == "down"
            and self.breakout_level is not None
            and c.get("epoch")
            != self.breakout_epoch
            and c["close"]
            > self.breakout_level
        ):
            event = "FAILED_BREAKOUT_LOW"
            level = self.breakout_level

            self.reversal_direction = "up"
            self.reversal_epoch = c.get(
                "epoch"
            )
            self.reversal_age = 0

        # -----------------------------------------------------
        # Direct rejection from previous high
        # -----------------------------------------------------
        elif (
            high_level is not None
            and c["high"]
            > high_level + tolerance
            and c["close"]
            < high_level
        ):
            event = "SWEEP_HIGH"
            level = high_level

            self.reversal_direction = "down"
            self.reversal_epoch = c.get(
                "epoch"
            )
            self.reversal_age = 0

        # -----------------------------------------------------
        # Direct rejection from previous low
        # -----------------------------------------------------
        elif (
            low_level is not None
            and c["low"]
            < low_level - tolerance
            and c["close"]
            > low_level
        ):
            event = "SWEEP_LOW"
            level = low_level

            self.reversal_direction = "up"
            self.reversal_epoch = c.get(
                "epoch"
            )
            self.reversal_age = 0

        # -----------------------------------------------------
        # Record clean breakout.
        # Do NOT treat it as reversal yet.
        # -----------------------------------------------------
        if (
            high_level is not None
            and c["high"]
            > high_level + tolerance
            and c["close"]
            >= high_level
        ):
            self.breakout_direction = "up"
            self.breakout_level = float(
                high_level
            )
            self.breakout_epoch = c.get(
                "epoch"
            )
            self.breakout_age = 0

        elif (
            low_level is not None
            and c["low"]
            < low_level - tolerance
            and c["close"]
            <= low_level
        ):
            self.breakout_direction = "down"
            self.breakout_level = float(
                low_level
            )
            self.breakout_epoch = c.get(
                "epoch"
            )
            self.breakout_age = 0

        if event is not None:
            self.liquidity_event = event
            self.last_liquidity_level = (
                float(level)
            )
        elif (
            self.reversal_direction is None
        ):
            self.liquidity_event = None

    # =========================================================
    # LIVE SNAPSHOT
    # =========================================================

    def snapshot(self, candle=None):
        if (
            candle is None
            and self.candles
        ):
            candle = self.candles[-1]

        if candle is None:
            return {
                "volatility": "UNKNOWN",
                "volatility_ratio": 0.0,
                "pressure": 0.0,
                "momentum": 0.0,
                "velocity": 0.0,
                "acceleration": 0.0,
                "candle_body_ratio": 0.0,
                "range_ratio": 0.0,
                "rejection": "NONE",
                "direction": None,
                "raw_direction": None,
                "reversal_direction": None,
                "liquidity_event": None,
                "liquidity_level": None,
                "score": 0,
            }

        avg = self._avg_range(20)

        current_range = max(
            float(candle["high"])
            - float(candle["low"]),
            0.0,
        )

        range_ratio = (
            current_range / avg
            if avg > 0
            else 0.0
        )

        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        cl = float(candle["close"])

        body = abs(cl - o)

        body_ratio = (
            body / current_range
            if current_range > 0
            else 0.0
        )

        upper = h - max(o, cl)
        lower = min(o, cl) - l

        if (
            lower > body * 1.5
            and lower > upper * 1.25
        ):
            rejection = "LOW_REJECTION"

        elif (
            upper > body * 1.5
            and upper > lower * 1.25
        ):
            rejection = "HIGH_REJECTION"

        else:
            rejection = "NONE"

        pressure = self._pressure(12)

        ticks = list(
            self.ticks
        )

        velocity = 0.0
        acceleration = 0.0

        if len(ticks) >= 4:
            e1, p1 = ticks[-4]
            e2, p2 = ticks[-1]

            velocity = (
                (p2 - p1)
                / max(e2 - e1, 1)
            )

        if len(ticks) >= 7:
            e1, p1 = ticks[-7]
            e2, p2 = ticks[-4]
            e3, p3 = ticks[-1]

            v1 = (
                (p2 - p1)
                / max(e2 - e1, 1)
            )

            v2 = (
                (p3 - p2)
                / max(e3 - e2, 1)
            )

            acceleration = v2 - v1

        raw_direction = (
            "up"
            if pressure >= 0.25
            else "down"
            if pressure <= -0.25
            else None
        )

        # A rejection at a known previous high/low has priority,
        # but only for a short time window.
        direction = (
            self.reversal_direction
            or raw_direction
        )

        momentum = pressure

        if (
            direction == "down"
            and self.reversal_direction
            == "down"
        ):
            momentum = min(
                momentum,
                -0.25,
            )

        elif (
            direction == "up"
            and self.reversal_direction
            == "up"
        ):
            momentum = max(
                momentum,
                0.25,
            )

        if range_ratio > 1.15:
            momentum *= 1.15

        if body_ratio > 0.60:
            momentum *= 1.10

        # -----------------------------------------------------
        # Volatility state
        # -----------------------------------------------------
        if range_ratio < 0.65:
            volatility = "LOW"

        elif range_ratio < 1.15:
            volatility = "NORMAL"

        elif range_ratio < 1.80:
            volatility = "EXPANDING"

        else:
            volatility = "EXTREME"

        # -----------------------------------------------------
        # Score
        # -----------------------------------------------------
        score = 50

        if volatility == "EXPANDING":
            score += 15

        elif volatility == "NORMAL":
            score += 5

        elif volatility == "LOW":
            score -= 20

        else:
            score -= 10

        score += min(
            20,
            int(abs(momentum) * 25),
        )

        if body_ratio >= 0.55:
            score += 8

        if rejection != "NONE":
            score += 5

        if self.liquidity_event:
            score += 10

        if self.reversal_direction:
            score += 10

        # Acceleration confirms that current movement is gaining
        # force, but does not independently create a signal.
        if avg > 0:
            acceleration_ratio = (
                abs(acceleration)
                / max(avg, 1e-12)
            )

            if acceleration_ratio >= 0.15:
                score += 5

        return {
            "volatility": volatility,
            "volatility_ratio": round(
                range_ratio,
                3,
            ),
            "pressure": round(
                pressure,
                3,
            ),
            "momentum": round(
                momentum,
                3,
            ),
            "velocity": round(
                velocity,
                6,
            ),
            "acceleration": round(
                acceleration,
                6,
            ),
            "candle_body_ratio": round(
                body_ratio,
                3,
            ),
            "range_ratio": round(
                range_ratio,
                3,
            ),
            "rejection": rejection,
            "direction": direction,
            "raw_direction": raw_direction,
            "reversal_direction": (
                self.reversal_direction
            ),
            "liquidity_event": (
                self.liquidity_event
            ),
            "liquidity_level": (
                self.last_liquidity_level
            ),
            "score": max(
                0,
                min(
                    100,
                    score,
                ),
            ),
        }
