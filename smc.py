import os
from collections import deque


class SMCAnalyzer:

    def __init__(self, symbol, max_candles=250):
        self.symbol = symbol
        self.candles = deque(maxlen=max_candles)

        self.trend = None

        # Latest sweep information.
        self.last_sweep = None
        self.last_sweep_epoch = None
        self.sweep_epochs = {
            "high": None,
            "low": None,
        }

        # Pending CHoCH + OB setup waiting for retest.
        self.pending_setup = None

        self._last_signal_epoch = None

        self.sweep_lookback = int(
            os.getenv("SMC_SWEEP_LOOKBACK", "5")
        )

        self.structure_lookback = int(
            os.getenv("SMC_STRUCTURE_LOOKBACK", "7")
        )

        self.displacement_lookback = int(
            os.getenv("SMC_DISPLACEMENT_LOOKBACK", "5")
        )

        self.displacement_body_ratio = float(
            os.getenv("SMC_MIN_BODY_RATIO", "0.60")
        )

        self.displacement_multiplier = float(
            os.getenv("SMC_DISPLACEMENT_MULTIPLIER", "1.20")
        )

        self.ob_search_candles = int(
            os.getenv("SMC_OB_SEARCH_CANDLES", "5")
        )

        self.retest_max_candles = int(
            os.getenv("SMC_RETEST_MAX_CANDLES", "5")
        )

    # ================================================================
    # TREND
    # ================================================================

    def _update_trend(self):
        if len(self.candles) < 6:
            return

        recent = list(self.candles)[-6:]

        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]

        higher_high = highs[-1] > highs[-3]
        higher_low = lows[-1] > lows[-3]

        lower_high = highs[-1] < highs[-3]
        lower_low = lows[-1] < lows[-3]

        if higher_high and higher_low:
            self.trend = "up"

        elif lower_high and lower_low:
            self.trend = "down"

    # ================================================================
    # LIQUIDITY SWEEP
    # ================================================================

    def _detect_sweep(self, candle):
        if len(self.candles) < self.sweep_lookback:
            return None

        previous = list(self.candles)[-self.sweep_lookback:]

        previous_high = max(
            c["high"] for c in previous
        )

        previous_low = min(
            c["low"] for c in previous
        )

        swept_high = (
            candle["high"] > previous_high
            and candle["close"] < previous_high
        )

        swept_low = (
            candle["low"] < previous_low
            and candle["close"] > previous_low
        )

        if swept_high and not swept_low:
            return "high"

        if swept_low and not swept_high:
            return "low"

        return None

    # ================================================================
    # DISPLACEMENT
    # ================================================================

    def _has_displacement(self, candle):
        body = abs(
            candle["close"] - candle["open"]
        )

        candle_range = (
            candle["high"] - candle["low"]
        )

        if candle_range <= 0:
            return False

        body_ratio = body / candle_range

        if body_ratio < self.displacement_body_ratio:
            return False

        if len(self.candles) < self.displacement_lookback:
            return False

        previous = list(self.candles)[
            -self.displacement_lookback:
        :]

        ranges = [
            c["high"] - c["low"]
            for c in previous
            if c["high"] > c["low"]
        ]

        if not ranges:
            return False

        average_range = sum(ranges) / len(ranges)

        if average_range <= 0:
            return False

        if candle_range < (
            average_range
            * self.displacement_multiplier
        ):
            return False

        return True

    # ================================================================
    # CHOCH
    # ================================================================

    def _detect_choch(self):
        required = self.structure_lookback + 1

        if len(self.candles) < required:
            return None

        candles = list(self.candles)

        current = candles[-1]

        window = candles[
            -(self.structure_lookback + 1):-1
        ]

        previous_high = max(
            c["high"] for c in window
        )

        previous_low = min(
            c["low"] for c in window
        )

        if current["close"] > previous_high:
            if self._has_displacement(current):
                return "up"

        if current["close"] < previous_low:
            if self._has_displacement(current):
                return "down"

        return None

    # ================================================================
    # ORDER BLOCK
    # ================================================================

    def _find_order_block(self, direction):
        candles = list(self.candles)

        if len(candles) < 3:
            return None

        # Current candle is the displacement candle.
        # Search only a small area immediately before it.
        search_start = max(
            0,
            len(candles)
            - 1
            - self.ob_search_candles,
        )

        candidates = candles[
            search_start:-1
        ]

        for candle in reversed(candidates):

            body = (
                candle["close"]
                - candle["open"]
            )

            candle_range = (
                candle["high"]
                - candle["low"]
            )

            if candle_range <= 0:
                continue

            body_ratio = (
                abs(body)
                / candle_range
            )

            # Bullish OB = bearish candle.
            if (
                direction == "up"
                and body < 0
                and body_ratio >= 0.20
            ):
                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

            # Bearish OB = bullish candle.
            if (
                direction == "down"
                and body > 0
                and body_ratio >= 0.20
            ):
                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

        return None

    # ================================================================
    # RETEST
    # ================================================================

    def _check_ob_retest(
        self,
        candle,
        setup,
    ):
        ob = setup["ob"]
        direction = setup["direction"]

        ob_high = ob["high"]
        ob_low = ob["low"]

        # Candle must actually enter/overlap the OB.
        touched = (
            candle["low"] <= ob_high
            and candle["high"] >= ob_low
        )

        if not touched:
            return False

        midpoint = (
            ob_high + ob_low
        ) / 2

        if direction == "up":

            # Bullish rejection:
            # price enters OB but closes back above midpoint.
            return (
                candle["close"] > midpoint
                and candle["close"] > candle["open"]
            )

        # Bearish rejection:
        # price enters OB but closes back below midpoint.
        return (
            candle["close"] < midpoint
            and candle["close"] < candle["open"]
        )

    # ================================================================
    # INVALIDATE OLD SETUP
    # ================================================================

    def _setup_invalidated(
        self,
        candle,
        setup,
    ):
        ob = setup["ob"]
        direction = setup["direction"]

        if direction == "up":
            # Strong close below bullish OB invalidates it.
            if candle["close"] < ob["low"]:
                return True

        else:
            # Strong close above bearish OB invalidates it.
            if candle["close"] > ob["high"]:
                return True

        return False

    # ================================================================
    # SWEEP ACCESS
    # ================================================================

    def get_sweep_epoch(self, side):
        return self.sweep_epochs.get(side)

    # ================================================================
    # ADD CANDLE
    # ================================================================

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

        if epoch is None:
            return None

        # ============================================================
        # LIVE CANDLE UPDATE
        # ============================================================

        if self.candles:

            last_epoch = (
                self.candles[-1].get("epoch")
            )

            if last_epoch == epoch:

                self.candles[-1] = c

                # Do not generate a new setup/signal from every tick.
                return None

        # ============================================================
        # NEW CANDLE
        # ============================================================

        sweep = self._detect_sweep(c)

        if sweep is not None:

            self.last_sweep = sweep
            self.last_sweep_epoch = epoch

            self.sweep_epochs[sweep] = epoch

        self.candles.append(c)

        self._update_trend()

        # ============================================================
        # EXISTING PENDING SETUP
        # ============================================================

        if self.pending_setup is not None:

            setup = self.pending_setup

            setup["bars_waited"] += 1

            # Do not test the CHoCH candle itself.
            if epoch != setup["choch_epoch"]:

                if self._setup_invalidated(
                    c,
                    setup,
                ):
                    self.pending_setup = None

                else:

                    if self._check_ob_retest(
                        c,
                        setup,
                    ):

                        if (
                            epoch
                            != self._last_signal_epoch
                        ):

                            self._last_signal_epoch = epoch

                            result = {
                                "direction": setup[
                                    "direction"
                                ],
                                "ob": setup["ob"],
                                "epoch": epoch,
                                "symbol": self.symbol,
                                "choch_epoch": setup[
                                    "choch_epoch"
                                ],
                                "sweep_epoch": setup[
                                    "sweep_epoch"
                                ],
                                "retest": True,
                            }

                            self.pending_setup = None

                            return result

            if (
                self.pending_setup is not None
                and setup["bars_waited"]
                >= self.retest_max_candles
            ):
                self.pending_setup = None

        # ============================================================
        # NEW CHOCH
        # ============================================================

        choch = self._detect_choch()

        if choch is None:
            return None

        # ============================================================
        # SWEEP MUST MATCH DIRECTION
        # ============================================================

        required_sweep = (
            "low"
            if choch == "up"
            else "high"
        )

        sweep_epoch = self.get_sweep_epoch(
            required_sweep
        )

        if sweep_epoch is None:
            return None

        # Sweep must be reasonably fresh.
        try:
            age = (
                float(epoch)
                - float(sweep_epoch)
            )
        except (TypeError, ValueError):
            return None

        max_sweep_age = (
            self.retest_max_candles
            * 60
        )

        if age < 0 or age > max_sweep_age:
            return None

        # ============================================================
        # VALID ORDER BLOCK
        # ============================================================

        ob = self._find_order_block(
            choch
        )

        if ob is None:
            return None

        # ============================================================
        # STORE SETUP
        #
        # Do NOT send signal yet.
        #
        # Price must return to OB and reject.
        # ============================================================

        self.pending_setup = {
            "direction": choch,
            "ob": ob,
            "choch_epoch": epoch,
            "sweep_epoch": sweep_epoch,
            "bars_waited": 0,
        }

        return None
