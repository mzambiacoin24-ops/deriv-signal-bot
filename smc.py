from collections import deque


class SMCAnalyzer:
    """Volatility-Index price-action engine.

    This is intentionally NOT a forex/crypto SMC clone. The M1 setup is
    treated as a sequence:

        CONTEXT -> LIQUIDITY -> DISPLACEMENT -> MSS -> FVG
        -> RETRACEMENT -> CONFIRMATION

    A liquidity sweep alone never creates a setup. The analyzer keeps the
    sequence alive for several candles so the signal can arrive after the
    reaction/retracement instead of chasing the first reversal candle.
    """

    def __init__(self, symbol, lookback=2, history=300):
        self.symbol = symbol
        self.lookback = max(1, int(lookback))
        self.candles = deque(maxlen=history)
        self.swing_highs = deque(maxlen=60)
        self.swing_lows = deque(maxlen=60)
        self.last_swing_high = None
        self.last_swing_low = None
        self.previous_swing_high = None
        self.previous_swing_low = None

        self.trend = None
        self.structure_strength = "NEUTRAL"
        self.bullish_score = 0
        self.bearish_score = 0

        self.last_event = None
        self.last_sweep = None
        self.sweep_age = None
        self.last_sweep_epoch = None
        self.last_liquidity_level = None
        self.last_breakout_level = None
        self.last_breakout_direction = None
        self.last_breakout_epoch = None
        self.breakout_direction = None
        self.breakout_level = None
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
        self.pending_stage = None
        self.pending_sweep_candle = None
        self.pending_displacement_epoch = None
        self.pending_mss_epoch = None
        self.pending_retracement_epoch = None
        self.pending_mss_level = None
        self.pending_target_liquidity = None

        self.last_setup = None
        self.last_setup_epoch = None
        self.last_break_epoch = None

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

        if self.candles and c.get("epoch") is not None and self.candles[-1].get("epoch") == c.get("epoch"):
            self.candles[-1] = c
            self._detect_liquidity_event(c)
            self._update_structure()
            return self._advance_narrative(c)

        self.candles.append(c)
        self._age_state()
        self._detect_confirmed_swing()
        self._detect_liquidity_event(c)
        self._update_structure()
        return self._advance_narrative(c)

    def _age_state(self):
        if self.sweep_age is not None:
            self.sweep_age += 1
            if self.sweep_age > 12:
                self.last_sweep = None
                self.last_sweep_epoch = None
                self.sweep_age = None

        if self.breakout_direction is not None:
            self.breakout_age += 1
            if self.breakout_age > 5:
                self._clear_breakout()

        if self.reversal_bias is not None:
            self.reversal_age += 1
            if self.reversal_age > 12:
                self.reversal_bias = None
                self.reversal_epoch = None
                self.reversal_age = 0

        if self.pending_direction is not None:
            self.pending_age += 1
            if self.pending_age > 12:
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

    def _recent_range_levels(self, count=20):
        candles = list(self.candles)
        if len(candles) < 6:
            return None, None
        previous = candles[-(count + 1):-1]
        if len(previous) < 5:
            previous = candles[:-1]
        if not previous:
            return None, None
        return max(float(c["high"]) for c in previous), min(float(c["low"]) for c in previous)

    def _major_levels(self):
        candles = list(self.candles)
        if len(candles) < 12:
            return None, None
        previous = candles[-61:-1]
        if len(previous) < 10:
            previous = candles[:-1]
        if not previous:
            return None, None
        return max(float(c["high"]) for c in previous), min(float(c["low"]) for c in previous)

    def _equal_high_level(self):
        highs = list(self.swing_highs)[-8:]
        if len(highs) < 2:
            return None
        tol = max(self._liquidity_tolerance(), 1e-12)
        for i in range(len(highs) - 1, 0, -1):
            if abs(highs[i] - highs[i - 1]) <= tol:
                return (highs[i] + highs[i - 1]) / 2.0
        return None

    def _equal_low_level(self):
        lows = list(self.swing_lows)[-8:]
        if len(lows) < 2:
            return None
        tol = max(self._liquidity_tolerance(), 1e-12)
        for i in range(len(lows) - 1, 0, -1):
            if abs(lows[i] - lows[i - 1]) <= tol:
                return (lows[i] + lows[i - 1]) / 2.0
        return None

    def _liquidity_levels(self):
        recent_high, recent_low = self._recent_range_levels(20)
        major_high, major_low = self._major_levels()
        return {
            "high": [x for x in (recent_high, major_high, self.last_swing_high, self._equal_high_level()) if x is not None],
            "low": [x for x in (recent_low, major_low, self.last_swing_low, self._equal_low_level()) if x is not None],
        }

    def _detect_liquidity_event(self, candle):
        if len(self.candles) < 7:
            return
        levels = self._liquidity_levels()
        tolerance = self._liquidity_tolerance()
        if not levels["high"] or not levels["low"]:
            return

        high_level = min(levels["high"], key=lambda x: abs(float(candle["high"]) - float(x)))
        low_level = min(levels["low"], key=lambda x: abs(float(candle["low"]) - float(x)))
        sweep = None
        level = None
        event = None

        if self.breakout_direction == "up" and self.breakout_level is not None and candle.get("epoch") != self.last_breakout_epoch and candle["close"] < self.breakout_level:
            sweep, level, event = "high", self.breakout_level, "FAILED_BREAKOUT_HIGH"
        elif self.breakout_direction == "down" and self.breakout_level is not None and candle.get("epoch") != self.last_breakout_epoch and candle["close"] > self.breakout_level:
            sweep, level, event = "low", self.breakout_level, "FAILED_BREAKOUT_LOW"
        elif candle["high"] > high_level + tolerance and candle["close"] < high_level:
            sweep, level, event = "high", high_level, "SWEEP_HIGH"
        elif candle["low"] < low_level - tolerance and candle["close"] > low_level:
            sweep, level, event = "low", low_level, "SWEEP_LOW"

        if candle["high"] > high_level + tolerance and candle["close"] >= high_level:
            self.last_breakout_direction = "up"
            self.last_breakout_level = float(high_level)
            self.last_breakout_epoch = candle.get("epoch")
            self.breakout_direction = "up"
            self.breakout_level = float(high_level)
            self.breakout_age = 0
        elif candle["low"] < low_level - tolerance and candle["close"] <= low_level:
            self.last_breakout_direction = "down"
            self.last_breakout_level = float(low_level)
            self.last_breakout_epoch = candle.get("epoch")
            self.breakout_direction = "down"
            self.breakout_level = float(low_level)
            self.breakout_age = 0

        if sweep is None:
            return
        if self.pending_direction in ("up", "down"):
            return

        self._clear_breakout()
        self.last_sweep = sweep
        self.sweep_age = 0
        self.last_sweep_epoch = candle.get("epoch")
        self.last_liquidity_level = float(level)
        self.reversal_bias = "down" if sweep == "high" else "up"
        self.reversal_epoch = candle.get("epoch")
        self.reversal_age = 0
        self.last_event = event
        self.last_break_epoch = candle.get("epoch")

        self._clear_pending_setup()
        self.pending_direction = self.reversal_bias
        self.pending_epoch = candle.get("epoch")
        self.pending_age = 0
        self.pending_kind = "VOLATILITY_NARRATIVE"
        self.pending_stage = "SWEEP"
        self.pending_sweep_candle = dict(candle)
        self.pending_mss_level = self._find_mss_level(self.pending_direction, candle)
        self.pending_target_liquidity = self._find_target_liquidity(self.pending_direction, float(candle["close"]))

    def _clear_breakout(self):
        self.breakout_direction = None
        self.breakout_level = None
        self.last_breakout_direction = None
        self.last_breakout_level = None
        self.last_breakout_epoch = None
        self.breakout_age = 0

    def _update_structure(self):
        highs = list(self.swing_highs)
        lows = list(self.swing_lows)
        candles = list(self.candles)
        bull = 0
        bear = 0
        if len(highs) >= 2:
            bull += 2 if highs[-1] > highs[-2] else 0
            bear += 2 if highs[-1] < highs[-2] else 0
        if len(lows) >= 2:
            bull += 2 if lows[-1] > lows[-2] else 0
            bear += 2 if lows[-1] < lows[-2] else 0
        if len(candles) >= 10:
            recent = candles[-10:]
            up_body = sum(max(0.0, c["close"] - c["open"]) for c in recent)
            down_body = sum(max(0.0, c["open"] - c["close"]) for c in recent)
            if up_body > down_body * 1.30:
                bull += 2
            elif down_body > up_body * 1.30:
                bear += 2
        self.bullish_score = bull
        self.bearish_score = bear
        if bull >= 4 and bull >= bear + 2:
            self.trend = "up"
            self.structure_strength = "STRONG" if bull >= 6 else "MODERATE"
        elif bear >= 4 and bear >= bull + 2:
            self.trend = "down"
            self.structure_strength = "STRONG" if bear >= 6 else "MODERATE"
        elif self.trend is None:
            self.structure_strength = "NEUTRAL"
        else:
            self.structure_strength = "MODERATE"

    def _find_mss_level(self, direction, sweep_candle):
        candles = list(self.candles)
        if direction == "up":
            candidates = [x for x in self.swing_highs if x < float(sweep_candle["high"])]
            if candidates:
                return float(candidates[-1])
            pre = candles[-7:-1]
            return max((float(c["high"]) for c in pre), default=None)
        candidates = [x for x in self.swing_lows if x > float(sweep_candle["low"])]
        if candidates:
            return float(candidates[-1])
        pre = candles[-7:-1]
        return min((float(c["low"]) for c in pre), default=None)

    def _displacement_ok(self, candle, direction):
        avg = self._average_range(20)
        rng = float(candle["high"]) - float(candle["low"])
        if rng <= 0:
            return False
        body_ratio = abs(float(candle["close"]) - float(candle["open"])) / rng
        if body_ratio < 0.55:
            return False
        if avg > 0 and rng < avg * 1.10:
            return False
        if avg > 0 and rng > avg * 2.80:
            return False
        return candle["close"] > candle["open"] if direction == "up" else candle["close"] < candle["open"]

    def _mss_confirmed(self, candle, direction):
        level = self.pending_mss_level
        if level is None:
            return False
        return float(candle["close"]) > level if direction == "up" else float(candle["close"]) < level

    def _detect_fvg_at_end(self, direction):
        candles = list(self.candles)
        if len(candles) < 3:
            return None
        a, _, c = candles[-3], candles[-2], candles[-1]
        if direction == "up" and float(c["low"]) > float(a["high"]):
            return {"direction": "up", "low": float(a["high"]), "high": float(c["low"]), "epoch": c.get("epoch")}
        if direction == "down" and float(c["high"]) < float(a["low"]):
            return {"direction": "down", "low": float(c["high"]), "high": float(a["low"]), "epoch": c.get("epoch")}
        return None

    def _find_target_liquidity(self, direction, entry):
        levels = self._liquidity_levels()
        if direction == "up":
            candidates = [float(x) for x in levels["high"] if float(x) > entry]
            return min(candidates) if candidates else None
        candidates = [float(x) for x in levels["low"] if float(x) < entry]
        return max(candidates) if candidates else None

    def _retracement_into_fvg(self, candle):
        if not self.pending_fvg:
            return False
        return float(candle["low"]) <= float(self.pending_fvg["high"]) and float(candle["high"]) >= float(self.pending_fvg["low"])

    def _confirmation_ok(self, candle, direction):
        rng = float(candle["high"]) - float(candle["low"])
        if rng <= 0:
            return False
        body_ratio = abs(float(candle["close"]) - float(candle["open"])) / rng
        if body_ratio < 0.35:
            return False
        avg = self._average_range(20)
        if avg > 0 and rng > avg * 2.40:
            return False
        if direction == "up":
            return candle["close"] > candle["open"] and candle["close"] >= float(candle["low"]) + rng * 0.55
        return candle["close"] < candle["open"] and candle["close"] <= float(candle["high"]) - rng * 0.55

    def _advance_narrative(self, candle):
        direction = self.pending_direction
        if direction not in ("up", "down"):
            return None
        if candle.get("epoch") == self.pending_epoch:
            return None
        if self.pending_age > 12:
            self._clear_pending_setup()
            return None

        if self.pending_stage == "SWEEP":
            if self._displacement_ok(candle, direction):
                self.pending_stage = "DISPLACEMENT"
                self.pending_displacement_epoch = candle.get("epoch")
                self.last_event = "DISPLACEMENT_AFTER_LIQUIDITY"
            return None

        if self.pending_stage == "DISPLACEMENT":
            if self._mss_confirmed(candle, direction):
                self.pending_stage = "MSS"
                self.pending_mss_epoch = candle.get("epoch")
                self.last_break_epoch = candle.get("epoch")
                self.last_event = "MSS_AFTER_DISPLACEMENT"
                fvg = self._detect_fvg_at_end(direction)
                if fvg is not None:
                    self.pending_fvg = fvg
                    self.pending_stage = "FVG"
                    self.last_event = "FVG_AFTER_MSS"
            return None

        if self.pending_stage == "MSS":
            fvg = self._detect_fvg_at_end(direction)
            if fvg is not None and fvg.get("epoch") == candle.get("epoch"):
                self.pending_fvg = fvg
                self.pending_stage = "FVG"
                self.last_event = "FVG_AFTER_MSS"
            return None

        if self.pending_stage == "FVG":
            if self._retracement_into_fvg(candle):
                self.pending_stage = "RETRACEMENT"
                self.pending_retracement_epoch = candle.get("epoch")
                self.last_event = "FVG_RETRACEMENT"
            return None

        if self.pending_stage == "RETRACEMENT":
            if not self._confirmation_ok(candle, direction):
                return None
            entry = float(candle["close"])
            target = self._find_target_liquidity(direction, entry) or self.pending_target_liquidity
            self.last_event = "VOLATILITY_NARRATIVE_CONFIRMED"
            self.trend = direction
            self.structure_strength = "STRONG"
            setup = {
                "direction": direction,
                "reason": "SSL_SWEEP_DISPLACEMENT_MSS_FVG_RETRACE_CONFIRM" if direction == "up" else "BSL_SWEEP_DISPLACEMENT_MSS_FVG_RETRACE_CONFIRM",
                "structure": self.trend,
                "strength": self.structure_strength,
                "ob": None,
                "fvg": dict(self.pending_fvg) if self.pending_fvg else None,
                "sweep": self.last_sweep,
                "sweep_epoch": self.last_sweep_epoch,
                "liquidity_level": self.last_liquidity_level,
                "displacement_epoch": self.pending_displacement_epoch,
                "mss_epoch": self.pending_mss_epoch,
                "mss_level": self.pending_mss_level,
                "retracement_epoch": self.pending_retracement_epoch,
                "confirmation_epoch": candle.get("epoch"),
                "target_liquidity": target,
                "score": 100,
                "timing": "SWEEP -> DISPLACEMENT -> MSS -> FVG -> RETRACEMENT -> CONFIRMATION",
                "event": self.last_event,
            }
            self.last_setup = setup
            self.last_setup_epoch = candle.get("epoch")
            self._clear_pending_setup()
            return setup
        return None

    def _clear_pending_setup(self):
        self.pending_ob = None
        self.pending_fvg = None
        self.pending_direction = None
        self.pending_epoch = None
        self.pending_age = 0
        self.pending_kind = None
        self.pending_stage = None
        self.pending_sweep_candle = None
        self.pending_displacement_epoch = None
        self.pending_mss_epoch = None
        self.pending_retracement_epoch = None
        self.pending_mss_level = None
        self.pending_target_liquidity = None

    def detect_structure_break(self):
        allowed = {
            "FAILED_BREAKOUT_HIGH", "FAILED_BREAKOUT_LOW",
            "DISPLACEMENT_AFTER_LIQUIDITY", "MSS_AFTER_DISPLACEMENT",
            "FVG_AFTER_MSS", "FVG_RETRACEMENT",
            "VOLATILITY_NARRATIVE_CONFIRMED",
        }
        return self.last_event if self.last_event in allowed else None

    def _find_order_block(self, direction):
        return None

    def _find_fvg(self, direction):
        return dict(self.pending_fvg) if self.pending_fvg and self.pending_fvg.get("direction") == direction else None

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
            "pending_stage": self.pending_stage,
            "pending_fvg": dict(self.pending_fvg) if self.pending_fvg else None,
            "pending_mss_level": self.pending_mss_level,
            "target_liquidity": self.pending_target_liquidity,
            "reversal_bias": self.reversal_bias,
            "last_breakout_level": self.last_breakout_level,
            "last_breakout_direction": self.last_breakout_direction,
        }

    def get_levels(self):
        recent_high, recent_low = self._recent_range_levels(20)
        major_high, major_low = self._major_levels()
        levels = self._liquidity_levels()
        return {
            "swing_highs": list(self.swing_highs),
            "swing_lows": list(self.swing_lows),
            "recent_liquidity_high": recent_high,
            "recent_liquidity_low": recent_low,
            "major_high": major_high,
            "major_low": major_low,
            "equal_high": self._equal_high_level(),
            "equal_low": self._equal_low_level(),
            "liquidity_highs": levels["high"],
            "liquidity_lows": levels["low"],
            "pending_ob": dict(self.pending_ob) if self.pending_ob else None,
            "pending_fvg": dict(self.pending_fvg) if self.pending_fvg else None,
            "pending_stage": self.pending_stage,
            "target_liquidity": self.pending_target_liquidity,
        }
