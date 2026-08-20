from collections import deque


class SMCAnalyzer:
    """Volatility-Index behaviour engine.

    Decision model: previous highs/lows, failed breaks, rejection,
    displacement and confirmation. Generic forex/crypto OB/FVG logic is
    deliberately not required for a Volatility setup.
    """

    def __init__(self, symbol, lookback=2, history=300):
        self.symbol = symbol
        self.lookback = max(1, int(lookback))
        self.candles = deque(maxlen=history)
        self.swing_highs = deque(maxlen=40)
        self.swing_lows = deque(maxlen=40)
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
        self.last_liquidity_level = None
        self.last_breakout_level = None
        self.last_breakout_direction = None
        self.last_breakout_epoch = None
        self.breakout_age = 0
        self.reversal_bias = None
        self.reversal_epoch = None
        self.reversal_age = 0
        self.pending_ob = None
        self.pending_fvg = None
        self.pending_direction = None
        self.pending_epoch = None
        self.pending_age = 0
        self.pending_kind = None
        self.last_setup = None
        self.last_setup_epoch = None
        self.last_break_epoch = None
        self.bullish_score = 0
        self.bearish_score = 0

    def add_candle(self, candle):
        for key in ("open", "high", "low", "close"):
            if key not in candle:
                raise ValueError("Candle must contain open, high, low and close")

        c = {
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "epoch": candle.get("epoch"),
        }
        epoch = c.get("epoch")

        if self.candles and epoch is not None and self.candles[-1].get("epoch") == epoch:
            self.candles[-1] = c
            self._detect_liquidity_event(c)
            self._update_structure()
            return self._check_reversal_confirmation(c)

        self.candles.append(c)
        self._age_state()
        self._detect_confirmed_swing()
        self._detect_liquidity_event(c)
        self._update_structure()
        return self._check_reversal_confirmation(c)

    def _age_state(self):
        if self.sweep_age is not None:
            self.sweep_age += 1
            if self.sweep_age > 6:
                self.last_sweep = None
                self.last_sweep_epoch = None
                self.sweep_age = None

        if self.breakout_direction is not None:
            self.breakout_age += 1
            if self.breakout_age > 5:
                self.last_breakout_level = None
                self.last_breakout_direction = None
                self.last_breakout_epoch = None
                self.breakout_age = 0

        if self.reversal_bias is not None:
            self.reversal_age += 1
            if self.reversal_age > 7:
                self.reversal_bias = None
                self.reversal_epoch = None
                self.reversal_age = 0

        if self.pending_direction is not None:
            self.pending_age += 1
            if self.pending_age > 8:
                self._clear_pending_setup()

    def _detect_confirmed_swing(self):
        n = len(self.candles)
        required = self.lookback * 2 + 1
        if n < required:
            return
        candles = list(self.candles)
        center_index = n - 1 - self.lookback
        if center_index < self.lookback:
            return
        center = candles[center_index]
        left = candles[center_index - self.lookback:center_index]
        right = candles[center_index + 1:center_index + 1 + self.lookback]
        if len(right) < self.lookback:
            return
        if all(center["high"] > x["high"] for x in left) and all(center["high"] > x["high"] for x in right):
            self._register_swing_high(center["high"])
        if all(center["low"] < x["low"] for x in left) and all(center["low"] < x["low"] for x in right):
            self._register_swing_low(center["low"])

    def _register_swing_high(self, price):
        price = float(price)
        if self.swing_highs and price == self.swing_highs[-1]:
            return
        self.previous_swing_high = self.last_swing_high
        self.last_swing_high = price
        self.swing_highs.append(price)

    def _register_swing_low(self, price):
        price = float(price)
        if self.swing_lows and price == self.swing_lows[-1]:
            return
        self.previous_swing_low = self.last_swing_low
        self.last_swing_low = price
        self.swing_lows.append(price)

    def _recent_range_levels(self, count=20):
        candles = list(self.candles)
        if len(candles) < 6:
            return None, None
        previous = candles[-(count + 1):-1]
        if len(previous) < 5:
            previous = candles[:-1]
        if not previous:
            return None, None
        return (
            max(float(c["high"]) for c in previous),
            min(float(c["low"]) for c in previous),
        )

    def _major_levels(self):
        candles = list(self.candles)
        if len(candles) < 12:
            return None, None
        previous = candles[-61:-1]
        if len(previous) < 10:
            previous = candles[:-1]
        if not previous:
            return None, None
        return (
            max(float(c["high"]) for c in previous),
            min(float(c["low"]) for c in previous),
        )

    def _average_range(self, count=20):
        values = [
            float(c["high"]) - float(c["low"])
            for c in list(self.candles)[-count:]
            if float(c["high"]) > float(c["low"])
        ]
        return sum(values) / len(values) if values else 0.0

    def _liquidity_tolerance(self):
        avg = self._average_range(20)
        return avg * 0.08 if avg > 0 else 0.0

    def _detect_liquidity_event(self, candle):
        if len(self.candles) < 7:
            return

        previous_high, previous_low = self._recent_range_levels(20)
        major_high, major_low = self._major_levels()
        tolerance = self._liquidity_tolerance()

        high_levels = [x for x in (previous_high, major_high, self.last_swing_high) if x is not None]
        low_levels = [x for x in (previous_low, major_low, self.last_swing_low) if x is not None]
        if not high_levels or not low_levels:
            return

        high_level = min(high_levels, key=lambda x: abs(float(candle["high"]) - float(x)))
        low_level = min(low_levels, key=lambda x: abs(float(candle["low"]) - float(x)))

        sweep = None
        level = None
        event = None

        if (
            self.breakout_direction == "up"
            and self.breakout_level is not None
            and candle.get("epoch") != self.last_breakout_epoch
            and candle["close"] < self.breakout_level - tolerance * 0.20
        ):
            sweep, level, event = "high", self.breakout_level, "FAILED_BREAKOUT_HIGH"
        elif (
            self.breakout_direction == "down"
            and self.breakout_level is not None
            and candle.get("epoch") != self.last_breakout_epoch
            and candle["close"] > self.breakout_level + tolerance * 0.20
        ):
            sweep, level, event = "low", self.breakout_level, "FAILED_BREAKOUT_LOW"
        elif candle["high"] > high_level + tolerance and candle["close"] < high_level:
            sweep, level, event = "high", high_level, "SWEEP_HIGH"
        elif candle["low"] < low_level - tolerance and candle["close"] > low_level:
            sweep, level, event = "low", low_level, "SWEEP_LOW"

        if candle["high"] > high_level + tolerance and candle["close"] >= high_level:
            self.last_breakout_direction = "up"
            self.last_breakout_level = float(high_level)
            self.last_breakout_epoch = candle.get("epoch")
            self.breakout_age = 0
        elif candle["low"] < low_level - tolerance and candle["close"] <= low_level:
            self.last_breakout_direction = "down"
            self.last_breakout_level = float(low_level)
            self.last_breakout_epoch = candle.get("epoch")
            self.breakout_age = 0

        if sweep is None:
            return

        self.last_sweep = sweep
        self.sweep_age = 0
        self.last_sweep_epoch = candle.get("epoch")
        self.last_liquidity_level = float(level)
        self.reversal_bias = "down" if sweep == "high" else "up"
        self.reversal_epoch = candle.get("epoch")
        self.reversal_age = 0
        self.last_event = event
        self.pending_direction = self.reversal_bias
        self.pending_epoch = candle.get("epoch")
        self.pending_age = 0
        self.pending_kind = "VOLATILITY_REVERSAL"
        self.pending_ob = {
            "direction": self.reversal_bias,
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "epoch": candle.get("epoch"),
        }
        self.pending_fvg = None

    def _update_structure(self):
        highs = list(self.swing_highs)
        lows = list(self.swing_lows)
        candles = list(self.candles)
        bull = 0
        bear = 0

        if len(highs) >= 2:
            if highs[-1] > highs[-2]:
                bull += 2
            elif highs[-1] < highs[-2]:
                bear += 2

        if len(lows) >= 2:
            if lows[-1] > lows[-2]:
                bull += 2
            elif lows[-1] < lows[-2]:
                bear += 2

        if len(candles) >= 8:
            recent = candles[-8:]
            up_body = sum(max(0.0, c["close"] - c["open"]) for c in recent)
            down_body = sum(max(0.0, c["open"] - c["close"]) for c in recent)
            if up_body > down_body * 1.25:
                bull += 2
            elif down_body > up_body * 1.25:
                bear += 2

        if self.reversal_bias == "down":
            bear += 5
        elif self.reversal_bias == "up":
            bull += 5

        self.bullish_score = bull
        self.bearish_score = bear

        if bull >= 4 and bull >= bear + 2:
            self.trend = "up"
            self.structure_strength = "STRONG" if bull >= 7 else "MODERATE"
        elif bear >= 4 and bear >= bull + 2:
            self.trend = "down"
            self.structure_strength = "STRONG" if bear >= 7 else "MODERATE"
        elif self.trend is None:
            self.structure_strength = "NEUTRAL"
        else:
            self.structure_strength = "MODERATE"

    def _check_reversal_confirmation(self, candle):
        direction = self.pending_direction
        if direction not in ("up", "down"):
            return None
        if self.pending_epoch is None or candle.get("epoch") == self.pending_epoch:
            return None
        if self.pending_age > 8:
            self._clear_pending_setup()
            return None

        r = float(candle["high"]) - float(candle["low"])
        if r <= 0:
            return None
        body_ratio = abs(float(candle["close"]) - float(candle["open"])) / r
        if body_ratio < 0.35:
            return None

        bullish = candle["close"] > candle["open"]
        bearish = candle["close"] < candle["open"]
        level = self.last_liquidity_level
        avg = self._average_range(20)

        if direction == "down":
            confirmed = bearish
            if level is not None:
                confirmed = confirmed and candle["close"] < level
            if not confirmed:
                return None
            if avg > 0 and r > avg * 1.80:
                return None
            self.trend = "down"
            self.structure_strength = "STRONG" if body_ratio >= 0.60 else "MODERATE"
        else:
            confirmed = bullish
            if level is not None:
                confirmed = confirmed and candle["close"] > level
            if not confirmed:
                return None
            if avg > 0 and r > avg * 1.80:
                return None
            self.trend = "up"
            self.structure_strength = "STRONG" if body_ratio >= 0.60 else "MODERATE"

        self.last_event = "VOLATILITY_REVERSAL_CONFIRMED"
        setup = {
            "direction": direction,
            "reason": "VOLATILITY_HIGH_REVERSAL" if direction == "down" else "VOLATILITY_LOW_REVERSAL",
            "structure": self.trend,
            "strength": self.structure_strength,
            "ob": None,
            "fvg": None,
            "sweep": self.last_sweep,
            "sweep_epoch": self.last_sweep_epoch,
            "liquidity_level": self.last_liquidity_level,
            "break_epoch": self.pending_epoch,
            "score": self.bearish_score if direction == "down" else self.bullish_score,
            "timing": "ZONE -> REJECTION -> CONFIRMATION",
            "event": self.last_event,
        }
        self.last_setup = setup
        self.last_setup_epoch = candle.get("epoch")
        self._clear_pending_setup()
        return setup

    def _clear_pending_setup(self):
        self.pending_ob = None
        self.pending_fvg = None
        self.pending_direction = None
        self.pending_epoch = None
        self.pending_age = 0
        self.pending_kind = None

    def detect_structure_break(self):
        if self.last_event in ("FAILED_BREAKOUT_HIGH", "FAILED_BREAKOUT_LOW", "VOLATILITY_REVERSAL_CONFIRMED"):
            return self.last_event
        return None

    def _find_order_block(self, direction):
        return None

    def _find_fvg(self, direction):
        return None

    def _price_near_zone(self, candle, zone):
        if not zone:
            return False
        high = float(zone["high"] if "high" in zone else zone["top"])
        low = float(zone["low"] if "low" in zone else zone["bottom"])
        return candle["low"] <= high and candle["high"] >= low

    def _near_recent_low(self, price, tolerance=0.0025):
        levels = list(self.swing_lows)[-3:]
        _, low = self._recent_range_levels(20)
        if low is not None:
            levels.append(low)
        return any(abs(price - level) / max(abs(level), 1e-7) <= tolerance for level in levels)

    def _near_recent_high(self, price, tolerance=0.0025):
        levels = list(self.swing_highs)[-3:]
        high, _ = self._recent_range_levels(20)
        if high is not None:
            levels.append(high)
        return any(abs(price - level) / max(abs(level), 1e-7) <= tolerance for level in levels)

    def _too_close_to_recent_high(self, price):
        high, _ = self._recent_range_levels(20)
        return high is not None and abs(high - price) / max(abs(high), 1e-7) <= 0.001

    def _too_close_to_recent_low(self, price):
        _, low = self._recent_range_levels(20)
        return low is not None and abs(price - low) / max(abs(low), 1e-7) <= 0.001

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
            "last_liquidity_level": self.last_liquidity_level,
            "last_swing_high": self.last_swing_high,
            "last_swing_low": self.last_swing_low,
            "pending_direction": self.pending_direction,
            "pending_epoch": self.pending_epoch,
            "pending_age": self.pending_age,
            "pending_kind": self.pending_kind,
            "reversal_bias": self.reversal_bias,
            "last_breakout_level": self.last_breakout_level,
            "last_breakout_direction": self.last_breakout_direction,
        }

    def get_levels(self):
        recent_high, recent_low = self._recent_range_levels(20)
        major_high, major_low = self._major_levels()
        return {
            "swing_highs": list(self.swing_highs),
            "swing_lows": list(self.swing_lows),
            "recent_liquidity_high": recent_high,
            "recent_liquidity_low": recent_low,
            "major_high": major_high,
            "major_low": major_low,
            "pending_ob": dict(self.pending_ob) if self.pending_ob else None,
            "pending_fvg": dict(self.pending_fvg) if self.pending_fvg else None,
        }
