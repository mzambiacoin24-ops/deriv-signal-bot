from collections import deque


class SMCAnalyzer:

    def __init__(self, symbol, max_candles=250):
        self.symbol = symbol
        self.candles = deque(maxlen=max_candles)

        self.trend = None
        self.last_sweep = None

        self._last_signal_epoch = None
        self._last_sweep_epoch = None

        # Market structure state
        self._structure = None
        self._last_broken_high = None
        self._last_broken_low = None

        # Number of candles a liquidity sweep remains valid.
        self._sweep_valid_candles = 5

    def _find_swings(self, candles, window=2):
        """
        Tambua swing highs na swing lows.

        Swing High:
        high yake iko juu kuliko highs za candles
        zinazomzunguka.

        Swing Low:
        low yake iko chini kuliko lows za candles
        zinazomzunguka.
        """

        swing_highs = []
        swing_lows = []

        if len(candles) < (window * 2 + 1):
            return swing_highs, swing_lows

        for i in range(
            window,
            len(candles) - window
        ):

            current = candles[i]

            current_high = float(
                current["high"]
            )

            current_low = float(
                current["low"]
            )

            left = candles[
                i - window:i
            ]

            right = candles[
                i + 1:i + 1 + window
            ]

            left_highs = [
                float(c["high"])
                for c in left
            ]

            right_highs = [
                float(c["high"])
                for c in right
            ]

            left_lows = [
                float(c["low"])
                for c in left
            ]

            right_lows = [
                float(c["low"])
                for c in right
            ]

            if (
                current_high >= max(left_highs)
                and current_high >= max(right_highs)
            ):

                swing_highs.append(
                    {
                        "price": current_high,
                        "epoch": current.get("epoch"),
                        "index": i,
                    }
                )

            if (
                current_low <= min(left_lows)
                and current_low <= min(right_lows)
            ):

                swing_lows.append(
                    {
                        "price": current_low,
                        "epoch": current.get("epoch"),
                        "index": i,
                    }
                )

        return swing_highs, swing_lows

    def _update_trend(self):
        """
        Tambua market structure kwa kutumia swing highs/lows.

        Bullish:
            HH + HL

        Bearish:
            LH + LL

        Hii ni bora kuliko kulinganisha candle ya mwisho
        na candle ya 3 positions nyuma.
        """

        candles = list(self.candles)

        if len(candles) < 12:
            return

        swing_highs, swing_lows = self._find_swings(
            candles,
            window=2,
        )

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return

        last_high = swing_highs[-1]
        previous_high = swing_highs[-2]

        last_low = swing_lows[-1]
        previous_low = swing_lows[-2]

        higher_high = (
            last_high["price"]
            > previous_high["price"]
        )

        higher_low = (
            last_low["price"]
            > previous_low["price"]
        )

        lower_high = (
            last_high["price"]
            < previous_high["price"]
        )

        lower_low = (
            last_low["price"]
            < previous_low["price"]
        )

        # ------------------------------------------------------------
        # BULLISH STRUCTURE
        # ------------------------------------------------------------

        if higher_high and higher_low:

            self._structure = "bullish"
            self.trend = "up"

            self._last_broken_high = (
                last_high["price"]
            )

            self._last_broken_low = (
                last_low["price"]
            )

            return

        # ------------------------------------------------------------
        # BEARISH STRUCTURE
        # ------------------------------------------------------------

        if lower_high and lower_low:

            self._structure = "bearish"
            self.trend = "down"

            self._last_broken_high = (
                last_high["price"]
            )

            self._last_broken_low = (
                last_low["price"]
            )

            return

        # ------------------------------------------------------------
        # CONTINUATION
        # ------------------------------------------------------------

        # Kama structure haijabadilika kikamilifu,
        # tunahifadhi trend ya mwisho badala ya kugeuza
        # direction kutokana na candle moja.

    def _detect_sweep(self, candle):
        """
        Tambua liquidity sweep dhidi ya swing levels
        za candles zilizopita.

        Sweep inahitaji:
        - price ivunje level
        - candle ifunge ndani ya level

        High sweep:
            high > previous swing high
            close < previous swing high

        Low sweep:
            low < previous swing low
            close > previous swing low
        """

        candles = list(self.candles)

        if len(candles) < 8:
            return None

        # Tumia candles zilizofungwa kabla ya current candle.
        history = candles[-8:]

        swing_highs, swing_lows = self._find_swings(
            history,
            window=2,
        )

        previous_high = None
        previous_low = None

        if swing_highs:
            previous_high = swing_highs[-1]["price"]

        if swing_lows:
            previous_low = swing_lows[-1]["price"]

        if previous_high is None:
            previous_high = max(
                c["high"]
                for c in history[:-1]
            )

        if previous_low is None:
            previous_low = min(
                c["low"]
                for c in history[:-1]
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

    def _find_order_block(self, direction):
        """
        Tafuta candle ya mwisho ya opposite direction
        kabla ya displacement.

        BUY:
            candle bearish ya mwisho

        SELL:
            candle bullish ya mwisho
        """

        candles = list(self.candles)

        if len(candles) < 3:
            return None

        # Tazama candles chache za mwisho tu.
        # Hii inazuia kuchukua OB ya zamani sana.
        search = candles[-8:-1]

        for candle in reversed(search):

            body = (
                candle["close"]
                - candle["open"]
            )

            if direction == "up" and body < 0:

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

            if direction == "down" and body > 0:

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get("epoch"),
                }

        return None

    def _detect_choch(self):
        """
        Tambua CHoCH kwa kuvunja recent swing structure.

        Muhimu:
        CHoCH haitumii tu max/min ya candles 6.

        Inahitaji break ya structural swing level.
        """

        candles = list(self.candles)

        if len(candles) < 12:
            return None

        swing_highs, swing_lows = self._find_swings(
            candles,
            window=2,
        )

        if not swing_highs or not swing_lows:
            return None

        current = candles[-1]

        current_close = float(
            current["close"]
        )

        recent_high = swing_highs[-1]
        recent_low = swing_lows[-1]

        # ------------------------------------------------------------
        # BULLISH BREAK
        # ------------------------------------------------------------

        if current_close > recent_high["price"]:

            # Kama tayari trend ni bullish, hii ni
            # continuation/BOS, lakini direction bado ni up.
            return "up"

        # ------------------------------------------------------------
        # BEARISH BREAK
        # ------------------------------------------------------------

        if current_close < recent_low["price"]:

            return "down"

        return None

    def _detect_displacement(self, direction):
        """
        Angalia kama candle ya mwisho ina displacement
        ya maana kuelekea direction.

        Hii si filter kali sana; inalenga kuzuia
        candle ndogo/noise kutengeneza direction.
        """

        candles = list(self.candles)

        if len(candles) < 5:
            return False

        current = candles[-1]

        body = abs(
            current["close"]
            - current["open"]
        )

        ranges = [
            abs(
                c["high"]
                - c["low"]
            )
            for c in candles[-5:-1]
        ]

        if not ranges:
            return False

        average_range = (
            sum(ranges)
            / len(ranges)
        )

        if average_range <= 0:
            return False

        # Body iwe angalau 25% ya average range.
        # Hii si filter kali sana ili signals zisipungue sana.
        minimum_body = (
            average_range * 0.25
        )

        if body < minimum_body:
            return False

        if direction == "up":

            return (
                current["close"]
                > current["open"]
            )

        return (
            current["close"]
            < current["open"]
        )

    def _clear_stale_sweep(self):
        """
        Sweep ya zamani isitumike milele.
        """

        if self._last_sweep_epoch is None:
            return

        candles = list(self.candles)

        if not candles:
            return

        current_epoch = candles[-1].get(
            "epoch"
        )

        if current_epoch is None:
            return

        try:

            age = (
                float(current_epoch)
                - float(self._last_sweep_epoch)
            )

        except (
            TypeError,
            ValueError,
        ):

            self.last_sweep = None
            self._last_sweep_epoch = None
            return

        # Candle epoch tofauti kwa LTF = 60 sec.
        # Sweep ikipita zaidi ya configured candles,
        # inakuwa stale.
        max_age = (
            self._sweep_valid_candles
            * 60
        )

        if age > max_age:

            self.last_sweep = None
            self._last_sweep_epoch = None

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

        # ============================================================
        # LIVE CANDLE UPDATE
        # ============================================================

        if (
            self.candles
            and epoch is not None
        ):

            last_epoch = (
                self.candles[-1].get("epoch")
            )

            if last_epoch == epoch:

                # Candle bado iko live.
                # Ibadilishe tu; usitengeneze signal mpya
                # kwa kila tick.
                self.candles[-1] = c

                return None

        # ============================================================
        # EPOCH REQUIRED
        # ============================================================

        if epoch is None:
            return None

        # ============================================================
        # NEW CANDLE
        #
        # Candle iliyokuwa mwisho sasa imefungwa.
        # Tumia closed candle kwa market structure analysis.
        # ============================================================

        previous_closed = None

        if self.candles:

            previous_closed = self.candles[-1]

        # ============================================================
        # APPEND NEW LIVE CANDLE
        # ============================================================

        self.candles.append(c)

        # ============================================================
        # MARKET STRUCTURE
        # ============================================================

        self._update_trend()

        # ============================================================
        # LIQUIDITY SWEEP
        #
        # Tumia candle iliyofungwa, si candle mpya inayofunguka.
        # ============================================================

        if previous_closed is not None:

            sweep = self._detect_sweep(
                previous_closed
            )

            if sweep is not None:

                self.last_sweep = sweep

                self._last_sweep_epoch = (
                    previous_closed.get("epoch")
                )

        self._clear_stale_sweep()

        # ============================================================
        # CHoCH / STRUCTURAL BREAK
        #
        # Tumia current live candle kwa ajili ya kuona
        # kama market imeanza kuvunja structure.
        #
        # Lakini signal identity itabaki kwenye current epoch.
        # ============================================================

        choch = self._detect_choch()

        if choch is None:
            return None

        # ============================================================
        # DISPLACEMENT
        # ============================================================

        if not self._detect_displacement(
            choch
        ):

            return None

        # ============================================================
        # DIRECTION QUALITY
        #
        # Usiruhusu random CHoCH ibadilishe direction ya
        # HTF bila structure yenye nguvu.
        # ============================================================

        if self.trend is not None:

            if (
                self.trend == "up"
                and choch == "down"
            ):

                # Bearish break ndani ya bullish structure
                # inaweza kuwa pullback/retracement.
                #
                # Usigeuze HTF bias hapa.
                # Signal ya opposite direction inahitaji
                # structure mpya ijengeke.
                return None

            if (
                self.trend == "down"
                and choch == "up"
            ):

                # Bullish break ndani ya bearish structure
                # inaweza kuwa pullback/retracement.
                #
                # Usigeuze HTF bias hapa.
                return None

        # ============================================================
        # ORDER BLOCK
        # ============================================================

        ob = self._find_order_block(
            choch
        )

        if ob is None:
            return None

        # ============================================================
        # HARD SMC DUPLICATE PROTECTION
        # ============================================================

        if epoch == self._last_signal_epoch:
            return None

        self._last_signal_epoch = epoch

        return {
            "direction": choch,
            "ob": ob,
            "epoch": epoch,
            "symbol": self.symbol,
                    }
