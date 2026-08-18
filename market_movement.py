import math
from collections import deque


class MovementEngine:
    """Tick-level movement/volatility context for synthetic Volatility Indices."""

    def __init__(self, max_ticks=240, max_candles=40):
        self.ticks = deque(maxlen=max_ticks)
        self.candles = deque(maxlen=max_candles)
        self.last_epoch = None

    def update_tick(self, price, epoch, candle):
        price = float(price)
        epoch = int(epoch)
        self.ticks.append((epoch, price))
        self.last_epoch = epoch
        if candle is not None:
            c = dict(candle)
            if self.candles and self.candles[-1].get("epoch") == c.get("epoch"):
                self.candles[-1] = c
            else:
                self.candles.append(c)
        return self.snapshot(candle)

    def _avg_range(self, count=20):
        values = []
        for c in list(self.candles)[-count:]:
            r = float(c["high"]) - float(c["low"])
            if r > 0:
                values.append(r)
        return sum(values) / len(values) if values else 0.0

    def _pressure(self, count=12):
        candles = list(self.candles)[-count:]
        if not candles:
            return 0.0
        up = down = 0.0
        for c in candles:
            r = max(float(c["high"]) - float(c["low"]), 1e-12)
            body = float(c["close"]) - float(c["open"])
            weight = min(abs(body) / r, 1.0)
            if body > 0:
                up += weight
            elif body < 0:
                down += weight
        total = up + down
        return (up - down) / total if total else 0.0

    def snapshot(self, candle=None):
        if candle is None and self.candles:
            candle = self.candles[-1]
        if candle is None:
            return {
                "volatility": "UNKNOWN", "volatility_ratio": 0.0,
                "pressure": 0.0, "momentum": 0.0,
                "velocity": 0.0, "acceleration": 0.0,
                "candle_body_ratio": 0.0, "range_ratio": 0.0,
                "rejection": "NONE", "direction": None, "score": 0,
            }

        avg = self._avg_range(20)
        current_range = max(float(candle["high"]) - float(candle["low"]), 0.0)
        range_ratio = current_range / avg if avg > 0 else 0.0
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        cl = float(candle["close"])
        body = abs(cl - o)
        body_ratio = body / current_range if current_range > 0 else 0.0
        upper = h - max(o, cl)
        lower = min(o, cl) - l

        if lower > body * 1.5 and lower > upper * 1.25:
            rejection = "LOW_REJECTION"
        elif upper > body * 1.5 and upper > lower * 1.25:
            rejection = "HIGH_REJECTION"
        else:
            rejection = "NONE"

        pressure = self._pressure(12)
        velocity = 0.0
        acceleration = 0.0
        ticks = list(self.ticks)
        if len(ticks) >= 4:
            e1, p1 = ticks[-4]
            e2, p2 = ticks[-1]
            velocity = (p2 - p1) / max(e2 - e1, 1)
        if len(ticks) >= 7:
            e1, p1 = ticks[-7]
            e2, p2 = ticks[-4]
            e3, p3 = ticks[-1]
            v1 = (p2 - p1) / max(e2 - e1, 1)
            v2 = (p3 - p2) / max(e3 - e2, 1)
            acceleration = v2 - v1

        momentum = pressure
        if range_ratio > 1.15:
            momentum *= 1.15
        if body_ratio > 0.60:
            momentum *= 1.10

        if range_ratio < 0.65:
            volatility = "LOW"
        elif range_ratio < 1.15:
            volatility = "NORMAL"
        elif range_ratio < 1.80:
            volatility = "EXPANDING"
        else:
            volatility = "EXTREME"

        direction = None
        if momentum >= 0.25:
            direction = "up"
        elif momentum <= -0.25:
            direction = "down"

        score = 50
        if volatility == "EXPANDING":
            score += 15
        elif volatility == "NORMAL":
            score += 5
        elif volatility == "LOW":
            score -= 20
        elif volatility == "EXTREME":
            score -= 10
        score += min(20, int(abs(momentum) * 25))
        if body_ratio >= 0.55:
            score += 8
        if rejection != "NONE":
            score += 5

        return {
            "volatility": volatility,
            "volatility_ratio": round(range_ratio, 3),
            "pressure": round(pressure, 3),
            "momentum": round(momentum, 3),
            "velocity": round(velocity, 6),
            "acceleration": round(acceleration, 6),
            "candle_body_ratio": round(body_ratio, 3),
            "range_ratio": round(range_ratio, 3),
            "rejection": rejection,
            "direction": direction,
            "score": max(0, min(100, score)),
        }
