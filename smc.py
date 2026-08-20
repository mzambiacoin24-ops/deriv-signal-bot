from collections import deque


class SMCAnalyzer:
    """Market-structure engine tuned for Deriv Volatility Indices.

    It separates directional structure from liquidity/reversal events and
    does not require a confirmed fractal swing before it can see an important
    recent high/low being swept.
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
            self._detect_liquidity_sweep(c)
            self._confirm_reversal(c)
            self._update_structure()
            return self._check_pullback_entry(c)

        if self.sweep_age is not None:
            self.sweep_age += 1
            if self.sweep_age > 5:
                self.last_sweep = None
                self.last_sweep_epoch = None
                self.sweep_age = None

        if self.breakout_age:
            self.breakout_age += 1
            if self.breakout_age > 4:
                self.last_breakout_level = None
                self.last_breakout_direction = None
                self.last_breakout_epoch = None
                self.breakout_age = 0

        if self.reversal_bias is not None:
            self.reversal_age += 1
            if self.reversal_age > 6:
                self.reversal_bias = None
                self.reversal_epoch = None
                self.reversal_age = 0

        if self.pending_direction is not None:
            self.pending_age += 1
            if self.pending_age > 10:
                self._clear_pending_setup()

        self.candles.append(c)
        self._detect_confirmed_swing()
        self._update_structure()
        self._detect_liquidity_sweep(c)
        self._confirm_reversal(c)
        self._detect_structure_break()
        self._update_structure()
        return self._check_pullback_entry(c)

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

    def _update_structure(self):
        highs = list(self.swing_highs)
        lows = list(self.swing_lows)
        self.bullish_score = 0
        self.bearish_score = 0

        if len(highs) >= 2:
            if highs[-1] > highs[-2]:
                self.bullish_score += 2
            elif highs[-1] < highs[-2]:
                self.bearish_score += 2

        if len(lows) >= 2:
            if lows[-1] > lows[-2]:
                self.bullish_score += 2
            elif lows[-1] < lows[-2]:
                self.bearish_score += 2

        candles = list(self.candles)
        if len(candles) >= 6:
            recent = candles[-6:]
            bull = sum(c["close"] > c["open"] for c in recent)
            bear = sum(c["close"] < c["open"] for c in recent)
            if bull >= 4:
                self.bullish_score += 1
            if bear >= 4:
                self.bearish_score += 1

        if self.reversal_bias == "down":
            self.bearish_score += 2
        elif self.reversal_bias == "up":
            self.bullish_score += 2

        if self.bullish_score >= 4 and self.bullish_score >= self.bearish_score + 2:
            self.trend = "up"
            self.structure_strength = "STRONG" if self.bullish_score >= 5 else "MODERATE"
        elif self.bearish_score >= 4 and self.bearish_score >= self.bullish_score + 2:
            self.trend = "down"
            self.structure_strength = "STRONG" if self.bearish_score >= 5 else "MODERATE"
        elif self.trend is None:
            self.structure_strength = "NEUTRAL"
        else:
            self.structure_strength = "MODERATE"

    def _recent_liquidity_levels(self):
        candles = list(self.candles)
        if len(candles) < 6:
            return None, None
        previous = candles[-21:-1]
        if len(previous) < 5:
            previous = candles[:-1]
        if not previous:
            return None, None
        return max(c["high"] for c in previous), min(c["low"] for c in previous)

    def _liquidity_tolerance(self):
        ranges = [c["high"] - c["low"] for c in list(self.candles)[-20:] if c["high"] > c["low"]]
        if not ranges:
            return 0.0
        return sum(ranges) / len(ranges) * 0.08

    def _detect_liquidity_sweep(self, candle):
        confirmed_high = self.last_swing_high
        confirmed_low = self.last_swing_low
        recent_high, recent_low = self._recent_liquidity_levels()
        tol = self._liquidity_tolerance()

        high_candidates = [x for x in (confirmed_high, recent_high) if x is not None]
        low_candidates = [x for x in (confirmed_low, recent_low) if x is not None]
        high_level = max(high_candidates) if high_candidates else None
        low_level = min(low_candidates) if low_candidates else None

        sweep = None
        level = None

        # Failed breakout: price first takes a prior high/low and closes
        # beyond it, then quickly returns through that level.
        if (
            self.last_breakout_direction == "up"
            and self.last_breakout_level is not None
            and candle.get("epoch") != self.last_breakout_epoch
            and candle["close"] < self.last_breakout_level
        ):
            sweep = "high"
            level = self.last_breakout_level
            self.last_event = "FAILED_BREAKOUT_HIGH"
        elif (
            self.last_breakout_direction == "down"
            and self.last_breakout_level is not None
            and candle.get("epoch") != self.last_breakout_epoch
            and candle["close"] > self.last_breakout_level
        ):
            sweep = "low"
            level = self.last_breakout_level
            self.last_event = "FAILED_BREAKOUT_LOW"
        elif high_level is not None and candle["high"] > high_level + tol and candle["close"] < high_level:
            sweep = "high"
            level = high_level
        elif low_level is not None and candle["low"] < low_level - tol and candle["close"] > low_level:
            sweep = "low"
            level = low_level

        # Record a fresh breakout so a following rejection can be recognized.
        if high_level is not None and candle["high"] > high_level + tol and candle["close"] >= high_level:
            self.last_breakout_direction = "up"
            self.last_breakout_level = float(high_level)
            self.last_breakout_epoch = candle.get("epoch")
            self.breakout_age = 0
        elif low_level is not None and candle["low"] < low_level - tol and candle["close"] <= low_level:
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
        if self.last_event not in ("FAILED_BREAKOUT_HIGH", "FAILED_BREAKOUT_LOW"):
            self.last_event = "SWEEP_HIGH" if sweep == "high" else "SWEEP_LOW"

        self.reversal_bias = "down" if sweep == "high" else "up"
        self.reversal_epoch = candle.get("epoch")
        self.reversal_age = 0

        self.pending_direction = self.reversal_bias
        self.pending_epoch = candle.get("epoch")
        self.pending_age = 0
        self.pending_kind = "LIQUIDITY_REVERSAL"
        self.pending_ob = {
            "direction": self.reversal_bias,
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "epoch": candle.get("epoch"),
        }
        self.pending_fvg = None

    def _confirm_reversal(self, candle):
        if self.reversal_bias not in ("up", "down"):
            return
        if self.reversal_epoch is not None and candle.get("epoch") == self.reversal_epoch:
            return
        if self.reversal_age > 6:
            return

        r = candle["high"] - candle["low"]
        if r <= 0:
            return
        body_ratio = abs(candle["close"] - candle["open"]) / r

        if self.reversal_bias == "down":
            confirmed = candle["close"] < candle["open"] and body_ratio >= 0.45
            if self.last_liquidity_level is not None:
                confirmed = confirmed and candle["close"] < self.last_liquidity_level
            if confirmed:
                self.trend = "down"
                self.structure_strength = "MODERATE"
                self.last_event = "LIQUIDITY_REVERSAL_DOWN"
                self.pending_direction = "down"
                self.pending_kind = "LIQUIDITY_REVERSAL"
                self.pending_epoch = candle.get("epoch")
                self.pending_age = 0
        else:
            confirmed = candle["close"] > candle["open"] and body_ratio >= 0.45
            if self.last_liquidity_level is not None:
                confirmed = confirmed and candle["close"] > self.last_liquidity_level
            if confirmed:
                self.trend = "up"
                self.structure_strength = "MODERATE"
                self.last_event = "LIQUIDITY_REVERSAL_UP"
                self.pending_direction = "up"
                self.pending_kind = "LIQUIDITY_REVERSAL"
                self.pending_epoch = candle.get("epoch")
                self.pending_age = 0

    def _detect_structure_break(self):
        if len(self.candles) < 5:
            return None
        candle = self.candles[-1]
        epoch = candle.get("epoch")

        if self.last_swing_high is not None and candle["close"] > self.last_swing_high:
            if epoch == self.last_break_epoch or not self._has_displacement("up"):
                return None
            old = self.trend
            self.trend = "up"
            self.last_event = "CHOCH_UP" if old == "down" else "BOS_UP"
            self.last_break_epoch = epoch
            self.pending_direction = "up"
            self.pending_epoch = epoch
            self.pending_age = 0
            self.pending_kind = "STRUCTURE_BREAK"
            self.pending_ob = self._find_order_block("up")
            self.pending_fvg = self._find_fvg("up")
            return self.last_event

        if self.last_swing_low is not None and candle["close"] < self.last_swing_low:
            if epoch == self.last_break_epoch or not self._has_displacement("down"):
                return None
            old = self.trend
            self.trend = "down"
            self.last_event = "CHOCH_DOWN" if old == "up" else "BOS_DOWN"
            self.last_break_epoch = epoch
            self.pending_direction = "down"
            self.pending_epoch = epoch
            self.pending_age = 0
            self.pending_kind = "STRUCTURE_BREAK"
            self.pending_ob = self._find_order_block("down")
            self.pending_fvg = self._find_fvg("down")
            return self.last_event

        return None

    def _has_displacement(self, direction):
        candles = list(self.candles)
        if len(candles) < 6:
            return False
        c = candles[-1]
        r = c["high"] - c["low"]
        if r <= 0:
            return False
        body_ratio = abs(c["close"] - c["open"]) / r
        previous = [x["high"] - x["low"] for x in candles[-6:-1] if x["high"] > x["low"]]
        if not previous:
            return False
        avg = sum(previous) / len(previous)
        directional = c["close"] > c["open"] if direction == "up" else c["close"] < c["open"]
        return directional and body_ratio >= 0.50 and r >= avg * 1.05

    def _find_order_block(self, direction):
        candles = list(self.candles)
        if len(candles) < 4:
            return None
        for c in reversed(candles[:-1][-8:]):
            if direction == "up" and c["close"] < c["open"]:
                return {"direction": "up", "high": c["high"], "low": c["low"], "epoch": c.get("epoch")}
            if direction == "down" and c["close"] > c["open"]:
                return {"direction": "down", "high": c["high"], "low": c["low"], "epoch": c.get("epoch")}
        return None

    def _find_fvg(self, direction):
        candles = list(self.candles)
        if len(candles) < 3:
            return None
        c1, c2, c3 = candles[-3:]
        if direction == "up" and c1["high"] < c3["low"]:
            return {"direction": "up", "bottom": c1["high"], "top": c3["low"], "epoch": c3.get("epoch")}
        if direction == "down" and c1["low"] > c3["high"]:
            return {"direction": "down", "bottom": c3["high"], "top": c1["low"], "epoch": c3.get("epoch")}
        return None

    def _check_pullback_entry(self, candle):
        direction = self.pending_direction
        if direction not in ("up", "down"):
            return None
        if self.pending_epoch is None or candle.get("epoch") == self.pending_epoch:
            return None
        if self.pending_age > 10:
            self._clear_pending_setup()
            return None

        r = candle["high"] - candle["low"]
        if r <= 0:
            return None
        body_ratio = abs(candle["close"] - candle["open"]) / r
        if body_ratio < 0.25:
            return None

        price = candle["close"]
        bullish = candle["close"] > candle["open"]
        bearish = candle["close"] < candle["open"]

        if direction == "up":
            if not bullish:
                return None
            touched = self._price_near_zone(candle, self.pending_ob) or self._price_near_zone(candle, self.pending_fvg)
            near_low = self._near_recent_low(price)
            if not (touched or near_low):
                return None
            if self._too_close_to_recent_high(price):
                return None
            reason = "BULLISH_LIQUIDITY_REVERSAL" if self.pending_kind == "LIQUIDITY_REVERSAL" else "BULLISH_PULLBACK"
        else:
            if not bearish:
                return None
            touched = self._price_near_zone(candle, self.pending_ob) or self._price_near_zone(candle, self.pending_fvg)
            near_high = self._near_recent_high(price)
            if not (touched or near_high):
                return None
            if self._too_close_to_recent_low(price):
                return None
            reason = "BEARISH_LIQUIDITY_REVERSAL" if self.pending_kind == "LIQUIDITY_REVERSAL" else "BEARISH_PULLBACK"

        setup = {
            "direction": direction,
            "reason": reason,
            "structure": self.trend,
            "strength": self.structure_strength,
            "ob": dict(self.pending_ob) if self.pending_ob else None,
            "fvg": dict(self.pending_fvg) if self.pending_fvg else None,
            "sweep": self.last_sweep,
            "sweep_epoch": self.last_sweep_epoch,
            "liquidity_level": self.last_liquidity_level,
            "break_epoch": self.pending_epoch,
            "score": self.bullish_score if direction == "up" else self.bearish_score,
            "timing": "CONFIRMED",
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

    def _price_near_zone(self, candle, zone):
        if not zone:
            return False
        if "high" in zone:
            high, low = float(zone["high"]), float(zone["low"])
        else:
            high, low = float(zone["top"]), float(zone["bottom"])
        return candle["low"] <= high and candle["high"] >= low

    def _near_recent_low(self, price, tolerance=0.0025):
        levels = list(self.swing_lows)[-3:]
        _, dynamic_low = self._recent_liquidity_levels()
        if dynamic_low is not None:
            levels.append(dynamic_low)
        for level in levels:
            if abs(price - level) / max(abs(level), 1e-7) <= tolerance:
                return True
        return False

    def _near_recent_high(self, price, tolerance=0.0025):
        levels = list(self.swing_highs)[-3:]
        dynamic_high, _ = self._recent_liquidity_levels()
        if dynamic_high is not None:
            levels.append(dynamic_high)
        for level in levels:
            if abs(price - level) / max(abs(level), 1e-7) <= tolerance:
                return True
        return False

    def _too_close_to_recent_high(self, price):
        levels = list(self.swing_highs)[-2:]
        dynamic_high, _ = self._recent_liquidity_levels()
        if dynamic_high is not None:
            levels.append(dynamic_high)
        return bool(levels) and abs(max(levels) - price) / max(abs(max(levels)), 1e-7) <= 0.001

    def _too_close_to_recent_low(self, price):
        levels = list(self.swing_lows)[-2:]
        _, dynamic_low = self._recent_liquidity_levels()
        if dynamic_low is not None:
            levels.append(dynamic_low)
        return bool(levels) and abs(price - min(levels)) / max(abs(min(levels)), 1e-7) <= 0.001

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
        dynamic_high, dynamic_low = self._recent_liquidity_levels()
        return {
            "swing_highs": list(self.swing_highs),
            "swing_lows": list(self.swing_lows),
            "recent_liquidity_high": dynamic_high,
            "recent_liquidity_low": dynamic_low,
            "pending_ob": dict(self.pending_ob) if self.pending_ob else None,
            "pending_fvg": dict(self.pending_fvg) if self.pending_fvg else None,
        }
