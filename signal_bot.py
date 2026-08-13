import asyncio
import logging
import os
import time
from collections import deque

from dotenv import load_dotenv

from public_client import PublicMarketClient
from indicators import sma, rsi
from telegram_notifier import TelegramNotifier


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("signal-bot")


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
)


# ============================================================
# SIGNAL SETTINGS
# ============================================================

HTF_GRANULARITY = int(
    os.getenv(
        "SIGNAL_HTF_GRANULARITY",
        "900",
    )
)

LTF_GRANULARITY = int(
    os.getenv(
        "SIGNAL_LTF_GRANULARITY",
        "60",
    )
)

CANDLE_COUNT = int(
    os.getenv(
        "SIGNAL_CANDLE_COUNT",
        "200",
    )
)


# ============================================================
# INDICATORS
# ============================================================

RSI_PERIOD = int(
    os.getenv(
        "SIGNAL_RSI_PERIOD",
        "14",
    )
)

RSI_OVERBOUGHT = float(
    os.getenv(
        "SIGNAL_RSI_OVERBOUGHT",
        "70",
    )
)

RSI_OVERSOLD = float(
    os.getenv(
        "SIGNAL_RSI_OVERSOLD",
        "30",
    )
)

SMA_TREND = int(
    os.getenv(
        "SIGNAL_SMA_TREND",
        "50",
    )
)


# ============================================================
# RISK
# ============================================================

ACCOUNT_BALANCE = float(
    os.getenv(
        "ACCOUNT_BALANCE",
        "10000",
    )
)

RISK_PERCENT_PER_TRADE = float(
    os.getenv(
        "RISK_PERCENT_PER_TRADE",
        "1",
    )
)

RR_RATIO = float(
    os.getenv(
        "RR_RATIO",
        "2",
    )
)

SL_BUFFER_PCT = float(
    os.getenv(
        "SL_BUFFER_PCT",
        "0.1",
    )
)


# ============================================================
# SMC SETTINGS
# ============================================================

SMC_SWEEP_LOOKBACK = int(
    os.getenv(
        "SMC_SWEEP_LOOKBACK",
        "5",
    )
)

SMC_STRUCTURE_LOOKBACK = int(
    os.getenv(
        "SMC_STRUCTURE_LOOKBACK",
        "7",
    )
)

SMC_DISPLACEMENT_LOOKBACK = int(
    os.getenv(
        "SMC_DISPLACEMENT_LOOKBACK",
        "5",
    )
)

SMC_MIN_BODY_RATIO = float(
    os.getenv(
        "SMC_MIN_BODY_RATIO",
        "0.60",
    )
)

SMC_DISPLACEMENT_MULTIPLIER = float(
    os.getenv(
        "SMC_DISPLACEMENT_MULTIPLIER",
        "1.20",
    )
)

SMC_OB_SEARCH_CANDLES = int(
    os.getenv(
        "SMC_OB_SEARCH_CANDLES",
        "5",
    )
)

SMC_RETEST_MAX_CANDLES = int(
    os.getenv(
        "SMC_RETEST_MAX_CANDLES",
        "5",
    )
)

SMT_MAX_AGE_CANDLES = int(
    os.getenv(
        "SMT_MAX_AGE_CANDLES",
        "5",
    )
)


# ============================================================
# POINT VALUES
# ============================================================

POINT_VALUES = {}

for _pair in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1",
).split(","):

    if "=" in _pair:

        _sym, _val = _pair.split(
            "=",
            1,
        )

        try:
            POINT_VALUES[
                _sym.strip()
            ] = float(
                _val.strip()
            )

        except ValueError:
            pass


# ============================================================
# SYMBOL PAIRS
# ============================================================

SYMBOL_PAIRS = [
    (
        "R_10",
        "1HZ10V",
        "Volatility 10 Index",
    ),
    (
        "R_25",
        "1HZ25V",
        "Volatility 25 Index",
    ),
    (
        "R_50",
        "1HZ50V",
        "Volatility 50 Index",
    ),
    (
        "R_75",
        "1HZ75V",
        "Volatility 75 Index",
    ),
    (
        "R_100",
        "1HZ100V",
        "Volatility 100 Index",
    ),
]


# ============================================================
# OHLC NORMALIZER
# ============================================================

def _to_ohlc(c):

    return {
        "open": float(c["open"]),
        "high": float(c["high"]),
        "low": float(c["low"]),
        "close": float(c["close"]),
        "epoch": c.get("epoch"),
        "granularity": c.get(
            "granularity"
        ),
        "is_new_candle": bool(
            c.get(
                "is_new_candle",
                False,
            )
        ),
    }


# ============================================================
# SMC ANALYZER
# ============================================================

class SMCAnalyzer:

    def __init__(
        self,
        symbol,
        max_candles=250,
    ):

        self.symbol = symbol

        self.candles = deque(
            maxlen=max_candles
        )

        self.trend = None

        self.last_sweep = None
        self.last_sweep_epoch = None

        self.sweep_epochs = {
            "high": None,
            "low": None,
        }

        self.pending_setup = None

        self._last_signal_epoch = None

    # ========================================================
    # TREND
    # ========================================================

    def _update_trend(self):

        if len(self.candles) < 6:
            return

        recent = list(
            self.candles
        )[-6:]

        highs = [
            c["high"]
            for c in recent
        ]

        lows = [
            c["low"]
            for c in recent
        ]

        higher_high = (
            highs[-1] > highs[-3]
        )

        higher_low = (
            lows[-1] > lows[-3]
        )

        lower_high = (
            highs[-1] < highs[-3]
        )

        lower_low = (
            lows[-1] < lows[-3]
        )

        if (
            higher_high
            and higher_low
        ):

            self.trend = "up"

        elif (
            lower_high
            and lower_low
        ):

            self.trend = "down"

    # ========================================================
    # LIQUIDITY SWEEP
    # ========================================================

    def _detect_sweep(
        self,
        candle,
    ):

        if (
            len(self.candles)
            < SMC_SWEEP_LOOKBACK
        ):
            return None

        previous = list(
            self.candles
        )[-SMC_SWEEP_LOOKBACK:]

        previous_high = max(
            c["high"]
            for c in previous
        )

        previous_low = min(
            c["low"]
            for c in previous
        )

        swept_high = (
            candle["high"]
            > previous_high
            and candle["close"]
            < previous_high
        )

        swept_low = (
            candle["low"]
            < previous_low
            and candle["close"]
            > previous_low
        )

        if (
            swept_high
            and not swept_low
        ):

            return "high"

        if (
            swept_low
            and not swept_high
        ):

            return "low"

        return None

    # ========================================================
    # DISPLACEMENT
    # ========================================================

    def _has_displacement(
        self,
        candle,
    ):

        body = abs(
            candle["close"]
            - candle["open"]
        )

        candle_range = (
            candle["high"]
            - candle["low"]
        )

        if candle_range <= 0:
            return False

        body_ratio = (
            body
            / candle_range
        )

        if (
            body_ratio
            < SMC_MIN_BODY_RATIO
        ):
            return False

        if (
            len(self.candles)
            < SMC_DISPLACEMENT_LOOKBACK
        ):
            return False

        previous = list(
            self.candles
        )[
            -SMC_DISPLACEMENT_LOOKBACK:
        ]

        ranges = [
            c["high"] - c["low"]
            for c in previous
            if c["high"] > c["low"]
        ]

        if not ranges:
            return False

        average_range = (
            sum(ranges)
            / len(ranges)
        )

        if average_range <= 0:
            return False

        if (
            candle_range
            < (
                average_range
                * SMC_DISPLACEMENT_MULTIPLIER
            )
        ):
            return False

        return True

    # ========================================================
    # CHOCH
    # ========================================================

    def _detect_choch(self):

        required = (
            SMC_STRUCTURE_LOOKBACK
            + 1
        )

        if (
            len(self.candles)
            < required
        ):
            return None

        candles = list(
            self.candles
        )

        current = candles[-1]

        window = candles[
            -(SMC_STRUCTURE_LOOKBACK + 1):
            -1
        ]

        previous_high = max(
            c["high"]
            for c in window
        )

        previous_low = min(
            c["low"]
            for c in window
        )

        if (
            current["close"]
            > previous_high
        ):

            if self._has_displacement(
                current
            ):
                return "up"

        if (
            current["close"]
            < previous_low
        ):

            if self._has_displacement(
                current
            ):
                return "down"

        return None

    # ========================================================
    # ORDER BLOCK
    # ========================================================

    def _find_order_block(
        self,
        direction,
    ):

        candles = list(
            self.candles
        )

        if len(candles) < 3:
            return None

        search_start = max(
            0,
            len(candles)
            - 1
            - SMC_OB_SEARCH_CANDLES,
        )

        candidates = candles[
            search_start:-1
        ]

        for candle in reversed(
            candidates
        ):

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

            if (
                direction == "up"
                and body < 0
                and body_ratio >= 0.20
            ):

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get(
                        "epoch"
                    ),
                }

            if (
                direction == "down"
                and body > 0
                and body_ratio >= 0.20
            ):

                return {
                    "high": candle["high"],
                    "low": candle["low"],
                    "epoch": candle.get(
                        "epoch"
                    ),
                }

        return None

    # ========================================================
    # OB RETEST
    # ========================================================

    def _check_ob_retest(
        self,
        candle,
        setup,
    ):

        ob = setup["ob"]

        direction = setup[
            "direction"
        ]

        ob_high = ob["high"]
        ob_low = ob["low"]

        touched = (
            candle["low"]
            <= ob_high
            and candle["high"]
            >= ob_low
        )

        if not touched:
            return False

        midpoint = (
            ob_high
            + ob_low
        ) / 2

        if direction == "up":

            return (
                candle["close"]
                > midpoint
                and candle["close"]
                > candle["open"]
            )

        return (
            candle["close"]
            < midpoint
            and candle["close"]
            < candle["open"]
        )

    # ========================================================
    # INVALIDATE SETUP
    # ========================================================

    def _setup_invalidated(
        self,
        candle,
        setup,
    ):

        ob = setup["ob"]

        direction = setup[
            "direction"
        ]

        if direction == "up":

            if (
                candle["close"]
                < ob["low"]
            ):
                return True

        else:

            if (
                candle["close"]
                > ob["high"]
            ):
                return True

        return False

    # ========================================================
    # SWEEP EPOCH
    # ========================================================

    def get_sweep_epoch(
        self,
        side,
    ):

        return self.sweep_epochs.get(
            side
        )

    # ========================================================
    # ADD CANDLE
    # ========================================================

    def add_candle(
        self,
        candle,
        bootstrap=False,
    ):

        required = (
            "open",
            "high",
            "low",
            "close",
        )

        for key in required:

            if key not in candle:

                raise ValueError(
                    "Candle must contain "
                    "open, high, low and close"
                )

        c = {
            "open": float(
                candle["open"]
            ),
            "high": float(
                candle["high"]
            ),
            "low": float(
                candle["low"]
            ),
            "close": float(
                candle["close"]
            ),
            "epoch": candle.get(
                "epoch"
            ),
        }

        epoch = c.get(
            "epoch"
        )

        if epoch is None:
            return None

        # ====================================================
        # LIVE CANDLE UPDATE
        # ====================================================

        if self.candles:

            last_epoch = (
                self.candles[-1].get(
                    "epoch"
                )
            )

            if (
                last_epoch == epoch
            ):

                self.candles[-1] = c

                return None

        # ====================================================
        # NEW CANDLE
        # ====================================================

        sweep = self._detect_sweep(
            c
        )

        if sweep is not None:

            self.last_sweep = sweep

            self.last_sweep_epoch = (
                epoch
            )

            self.sweep_epochs[
                sweep
            ] = epoch

        self.candles.append(
            c
        )

        self._update_trend()

        # ====================================================
        # STARTUP / BOOTSTRAP
        #
        # Historical candles only build context.
        # They NEVER create pending setups or signals.
        # ====================================================

        if bootstrap:

            self.pending_setup = None

            return None

        # ====================================================
        # EXISTING PENDING SETUP
        # ====================================================

        if (
            self.pending_setup
            is not None
        ):

            setup = (
                self.pending_setup
            )

            setup[
                "bars_waited"
            ] += 1

            if (
                epoch
                != setup[
                    "choch_epoch"
                ]
            ):

                if self._setup_invalidated(
                    c,
                    setup,
                ):

                    self.pending_setup = (
                        None
                    )

                else:

                    if self._check_ob_retest(
                        c,
                        setup,
                    ):

                        if (
                            epoch
                            != self._last_signal_epoch
                        ):

                            self._last_signal_epoch = (
                                epoch
                            )

                            result = {
                                "direction": setup[
                                    "direction"
                                ],
                                "ob": setup[
                                    "ob"
                                ],
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

                            self.pending_setup = (
                                None
                            )

                            return result

            if (
                self.pending_setup
                is not None
                and setup[
                    "bars_waited"
                ]
                >= SMC_RETEST_MAX_CANDLES
            ):

                self.pending_setup = (
                    None
                )

        # ====================================================
        # NEW CHOCH
        # ====================================================

        choch = (
            self._detect_choch()
        )

        if choch is None:
            return None

        # ====================================================
        # REQUIRED SWEEP
        # ====================================================

        required_sweep = (
            "low"
            if choch == "up"
            else "high"
        )

        sweep_epoch = (
            self.get_sweep_epoch(
                required_sweep
            )
        )

        if sweep_epoch is None:

            log.info(
                "[%s] CHoCH rejected: "
                "required %s sweep not found.",
                self.symbol,
                required_sweep,
            )

            return None

        # ====================================================
        # SWEEP FRESHNESS
        # ====================================================

        try:

            age = (
                float(epoch)
                - float(sweep_epoch)
            )

        except (
            TypeError,
            ValueError,
        ):

            return None

        max_sweep_age = (
            SMC_RETEST_MAX_CANDLES
            * LTF_GRANULARITY
        )

        if (
            age < 0
            or age > max_sweep_age
        ):

            log.info(
                "[%s] CHoCH rejected: "
                "sweep too old.",
                self.symbol,
            )

            return None

        # ====================================================
        # VALID OB
        # ====================================================

        ob = (
            self._find_order_block(
                choch
            )
        )

        if ob is None:

            log.info(
                "[%s] CHoCH rejected: "
                "valid OB not found.",
                self.symbol,
            )

            return None

        # ====================================================
        # WAIT FOR OB RETEST
        # ====================================================

        self.pending_setup = {
            "direction": choch,
            "ob": ob,
            "choch_epoch": epoch,
            "sweep_epoch": sweep_epoch,
            "bars_waited": 0,
        }

        log.info(
            "[%s] QUALITY SETUP CREATED -> "
            "%s | Sweep=%s | CHoCH=%s | "
            "OB %.4f-%.4f | WAITING RETEST",
            self.symbol,
            choch.upper(),
            sweep_epoch,
            epoch,
            ob["low"],
            ob["high"],
        )

        return None


# ============================================================
# SIGNAL TRACKER
# ============================================================

class SignalTracker:

    """
    Hakuna GLOBAL SIGNAL LOCK.

    Kila signal ina TP/SL tracking yake.
    """

    def __init__(self):

        self._lock = asyncio.Lock()

        self.active_signals = []

    async def reserve(
        self,
        symbol,
        display_name,
        direction,
        entry,
        tp,
        sl,
        signal_epoch,
    ):

        async with self._lock:

            signal = {
                "symbol": symbol,
                "display_name": display_name,
                "direction": direction,
                "entry": float(entry),
                "tp": float(tp),
                "sl": float(sl),
                "signal_epoch": signal_epoch,
                "created_at": time.time(),
            }

            self.active_signals.append(
                signal
            )

            log.info(
                "[TRACKER] ADD -> %s %s | "
                "Entry %.4f | TP %.4f | SL %.4f | "
                "Active=%d",
                display_name,
                direction.upper(),
                entry,
                tp,
                sl,
                len(
                    self.active_signals
                ),
            )

            return True

    async def remove_signal(
        self,
        signal_epoch,
        symbol,
    ):

        async with self._lock:

            self.active_signals = [
                signal
                for signal
                in self.active_signals
                if not (
                    signal[
                        "signal_epoch"
                    ]
                    == signal_epoch
                    and signal[
                        "symbol"
                    ]
                    == symbol
                )
            ]

    async def check_and_close(
        self,
        symbol,
        candle,
    ):

        async with self._lock:

            if not self.active_signals:
                return []

            candle_epoch = candle.get(
                "epoch"
            )

            try:

                high = float(
                    candle["high"]
                )

                low = float(
                    candle["low"]
                )

            except (
                TypeError,
                ValueError,
                KeyError,
            ):

                return []

            results = []

            remaining = []

            for active in (
                self.active_signals
            ):

                if (
                    active["symbol"]
                    != symbol
                ):

                    remaining.append(
                        active
                    )

                    continue

                signal_epoch = (
                    active.get(
                        "signal_epoch"
                    )
                )

                # Never evaluate
                # signal creation candle.
                if (
                    signal_epoch
                    is not None
                    and candle_epoch
                    is not None
                ):

                    try:

                        if (
                            float(
                                candle_epoch
                            )
                            <= float(
                                signal_epoch
                            )
                        ):

                            remaining.append(
                                active
                            )

                            continue

                    except (
                        TypeError,
                        ValueError,
                    ):

                        remaining.append(
                            active
                        )

                        continue

                if (
                    active[
                        "direction"
                    ]
                    == "up"
                ):

                    tp_hit = (
                        high
                        >= active["tp"]
                    )

                    sl_hit = (
                        low
                        <= active["sl"]
                    )

                else:

                    tp_hit = (
                        low
                        <= active["tp"]
                    )

                    sl_hit = (
                        high
                        >= active["sl"]
                    )

                if (
                    not tp_hit
                    and not sl_hit
                ):

                    remaining.append(
                        active
                    )

                    continue

                if (
                    tp_hit
                    and sl_hit
                ):

                    result = (
                        "AMBIGUOUS"
                    )

                    hit_price = None

                elif tp_hit:

                    result = "TP"

                    hit_price = (
                        active["tp"]
                    )

                else:

                    result = "SL"

                    hit_price = (
                        active["sl"]
                    )

                results.append(
                    {
                        **active,
                        "result": result,
                        "hit_price": hit_price,
                        "candle_epoch": candle_epoch,
                    }
                )

            self.active_signals = (
                remaining
            )

            return results


# ============================================================
# PAIR MONITOR
# ============================================================

class PairMonitor:

    def __init__(
        self,
        primary_symbol,
        secondary_symbol,
        display_name,
        telegram,
        signal_tracker,
    ):

        self.primary_symbol = (
            primary_symbol
        )

        self.secondary_symbol = (
            secondary_symbol
        )

        self.display_name = (
            display_name
        )

        self.telegram = telegram

        self.signal_tracker = (
            signal_tracker
        )

        self.htf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf_secondary = (
            SMCAnalyzer(
                secondary_symbol
            )
        )

        self.ltf_closes = deque(
            maxlen=max(
                RSI_PERIOD,
                SMA_TREND,
            ) + 5
        )

        self.point_value = (
            POINT_VALUES.get(
                primary_symbol
            )
        )

        self._last_signal_candle_epoch = (
            None
        )

    # ========================================================
    # CANDLE HANDLER
    # ========================================================

    async def on_candle(
        self,
        symbol,
        ohlc,
    ):

        try:

            granularity = int(
                ohlc.get(
                    "granularity",
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return

        c = _to_ohlc(
            ohlc
        )

        # ====================================================
        # HTF
        # ====================================================

        if (
            symbol
            == self.primary_symbol
            and granularity
            == HTF_GRANULARITY
        ):

            self.htf.add_candle(
                c
            )

            return

        # ====================================================
        # PRIMARY LTF
        # ====================================================

        if (
            symbol
            == self.primary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            # =================================================
            # TP / SL TRACKING
            # =================================================

            results = (
                await self.signal_tracker.check_and_close(
                    self.primary_symbol,
                    c,
                )
            )

            for result in results:

                await self._notify_signal_result(
                    result
                )

            # =================================================
            # INDICATORS
            # =================================================

            self.ltf_closes.append(
                c["close"]
            )

            entry = (
                self.ltf.add_candle(
                    c
                )
            )

            if not entry:
                return

            signal_epoch = (
                entry.get(
                    "epoch"
                )
            )

            if signal_epoch is None:
                return

            # =================================================
            # PER-CANDLE DEDUPE
            # =================================================

            if (
                signal_epoch
                == self._last_signal_candle_epoch
            ):

                return

            sent = (
                await self._maybe_send_signal(
                    entry,
                    c["close"],
                    signal_epoch,
                )
            )

            if sent:

                self._last_signal_candle_epoch = (
                    signal_epoch
                )

            return

        # ====================================================
        # SECONDARY SMT PAIR
        # ====================================================

        if (
            symbol
            == self.secondary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            self.ltf_secondary.add_candle(
                c
            )

    # ========================================================
    # TP / SL NOTIFICATION
    # ========================================================

    async def _notify_signal_result(
        self,
        result,
    ):

        direction_text = (
            "BUY"
            if result[
                "direction"
            ]
            == "up"
            else "SELL"
        )

        if (
            result["result"]
            == "TP"
        ):

            text = (
                "🎯 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🎯 Take Profit: <b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                "✅ Signal imefikia TP.\n"
                "Bot inaendelea kutafuta signals."
            )

        elif (
            result["result"]
            == "SL"
        ):

            text = (
                "🛑 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🛑 Stop Loss: <b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                "⚠️ Signal imefikia SL.\n"
                "Bot inaendelea kutafuta signals."
            )

        else:

            text = (
                "⚠️ <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                "⚠️ TP na SL zote ziliguswa "
                "ndani ya candle moja.\n\n"
                "Bot inaendelea kutafuta signals."
            )

        try:

            await self.telegram.send(
                text
            )

        except Exception as e:

            log.error(
                "[%s] TP/SL notification failed: %s",
                self.display_name,
                e,
            )

    # ========================================================
    # SIGNAL QUALITY
    # ========================================================

    async def _maybe_send_signal(
        self,
        entry,
        price,
        signal_epoch,
    ):

        direction = entry[
            "direction"
        ]

        ob = entry[
            "ob"
        ]

        # ====================================================
        # HTF BIAS
        # ====================================================

        if (
            self.htf.trend
            is None
            or self.htf.trend
            != direction
        ):

            log.info(
                "[%s] QUALITY FAIL -> HTF bias.",
                self.display_name,
            )

            return False

        # ====================================================
        # RSI
        # ====================================================

        rsi_val = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        # ====================================================
        # SMA
        # ====================================================

        sma_val = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        if (
            rsi_val is None
            or sma_val is None
        ):

            return False

        if direction == "up":

            if (
                rsi_val
                >= RSI_OVERBOUGHT
            ):

                return False

            if price < sma_val:

                return False

        else:

            if (
                rsi_val
                <= RSI_OVERSOLD
            ):

                return False

            if price > sma_val:

                return False

        # ====================================================
        # SMT QUALITY FILTER
        # ====================================================

        required_sweep = (
            "low"
            if direction == "up"
            else "high"
        )

        primary_sweep_epoch = (
            self.ltf.get_sweep_epoch(
                required_sweep
            )
        )

        if (
            primary_sweep_epoch
            is None
        ):

            log.info(
                "[%s] QUALITY FAIL -> "
                "No primary sweep.",
                self.display_name,
            )

            return False

        # ====================================================
        # FRESH PRIMARY SWEEP
        # ====================================================

        try:

            sweep_age = (
                float(
                    signal_epoch
                )
                - float(
                    primary_sweep_epoch
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return False

        max_age_seconds = (
            SMT_MAX_AGE_CANDLES
            * LTF_GRANULARITY
        )

        if (
            sweep_age < 0
            or sweep_age
            > max_age_seconds
        ):

            log.info(
                "[%s] QUALITY FAIL -> "
                "Primary sweep too old.",
                self.display_name,
            )

            return False

        # ====================================================
        # SECONDARY SMT
        # ====================================================

        secondary_sweep_epoch = (
            self.ltf_secondary.get_sweep_epoch(
                required_sweep
            )
        )

        if (
            secondary_sweep_epoch
            is not None
        ):

            try:

                secondary_age = (
                    float(
                        signal_epoch
                    )
                    - float(
                        secondary_sweep_epoch
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                secondary_age = (
                    max_age_seconds
                    + 1
                )

            if (
                secondary_age >= 0
                and secondary_age
                <= max_age_seconds
            ):

                log.info(
                    "[%s] QUALITY FAIL -> "
                    "Secondary made same-side sweep.",
                    self.display_name,
                )

                return False

        # ====================================================
        # SMT PASS
        # ====================================================

        smt_note = (
            "✅ SMT divergence imethibitika "
            "(primary liquidity sweep + "
            "secondary HAKUFANYA same-side sweep)."
        )

        # ====================================================
        # TP / SL
        # ====================================================

        buffer = (
            price
            * (
                SL_BUFFER_PCT
                / 100
            )
        )

        if direction == "up":

            sl_price = (
                ob["low"]
                - buffer
            )

        else:

            sl_price = (
                ob["high"]
                + buffer
            )

        sl_distance = abs(
            price
            - sl_price
        )

        if sl_distance <= 0:
            return False

        if direction == "up":

            tp_price = (
                price
                + (
                    RR_RATIO
                    * sl_distance
                )
            )

        else:

            tp_price = (
                price
                - (
                    RR_RATIO
                    * sl_distance
                )
            )

        # ====================================================
        # LOT
        # ====================================================

        if (
            self.point_value
            and self.point_value > 0
        ):

            risk_amount = (
                ACCOUNT_BALANCE
                * (
                    RISK_PERCENT_PER_TRADE
                    / 100
                )
            )

            lot = (
                risk_amount
                / (
                    sl_distance
                    * self.point_value
                )
            )

            lot = max(
                round(
                    lot,
                    2,
                ),
                0.01,
            )

            lot_line = (
                "📊 Lot Size (pendekezo): "
                f"<b>{lot}</b>\n"
            )

        else:

            lot_line = (
                "📊 Lot Size: weka "
                "POINT_VALUES kwenye .env\n"
            )

        # ====================================================
        # TRACK
        # ====================================================

        reserved = (
            await self.signal_tracker.reserve(
                symbol=self.primary_symbol,
                display_name=self.display_name,
                direction=direction,
                entry=price,
                tp=tp_price,
                sl=sl_price,
                signal_epoch=signal_epoch,
            )
        )

        if not reserved:
            return False

        emoji = (
            "📈"
            if direction == "up"
            else "📉"
        )

        action = (
            "NUNUA (BUY)"
            if direction == "up"
            else "UZA (SELL)"
        )

        # ====================================================
        # TELEGRAM
        # ====================================================

        try:

            await self.telegram.send(
                f"{emoji} <b>ISHARA: {action}</b>\n"
                f"Symbol (MT5): "
                f"<b>{self.display_name}</b>\n"
                f"Bei ya kuingia: "
                f"{price:.4f}\n"
                f"🎯 Take Profit: "
                f"{tp_price:.4f}\n"
                f"🛑 Stop Loss: "
                f"{sl_price:.4f}\n"
                f"{lot_line}"
                f"Muundo: HTF(15m) "
                f"bias={self.htf.trend.upper()} + "
                f"LTF(1m) CHoCH + "
                f"Displacement + OB Retest\n"
                f"RSI(14): {rsi_val:.1f} | "
                f"Bei dhidi ya SMA{SMA_TREND}: "
                f"{'juu' if price > sma_val else 'chini'}\n"
                f"{smt_note}\n\n"
                "⭐ <b>QUALITY SIGNAL</b>\n"
                "Liquidity Sweep ✓\n"
                "Displacement ✓\n"
                "CHoCH ✓\n"
                "Valid Order Block ✓\n"
                "OB Retest ✓\n"
                "HTF Bias ✓\n"
                "RSI/SMA ✓\n"
                "SMT Divergence ✓\n\n"
                "🔓 <b>TP/SL TRACKING: ACTIVE</b>\n"
                "Signal hii itafuatiliwa hadi TP au SL, "
                "lakini haitazuia signal nyingine.\n\n"
                "⚠️ Hii ni PENDEKEZO TU "
                "(si ushauri wa kifedha)."
            )

        except Exception:

            await self.signal_tracker.remove_signal(
                signal_epoch,
                self.primary_symbol,
            )

            raise

        log.info(
            "[%s] QUALITY SIGNAL SENT | "
            "direction=%s | epoch=%s | "
            "Entry %.4f | TP %.4f | SL %.4f",
            self.display_name,
            direction.upper(),
            signal_epoch,
            price,
            tp_price,
            sl_price,
        )

        return True


# ============================================================
# RUN PAIR
# ============================================================

async def run_pair(
    monitor,
):

    client = (
        PublicMarketClient()
    )

    client.on_candle = (
        monitor.on_candle
    )

    backoff = 5
    max_backoff = 300

    while True:

        started_at = (
            time.time()
        )

        try:

            await client.connect()

            # ====================================================
            # HTF HISTORY
            #
            # BOOTSTRAP = TRUE
            # ====================================================

            htf_hist = (
                await client.get_candle_history(
                    monitor.primary_symbol,
                    HTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )

            for c in htf_hist:

                monitor.htf.add_candle(
                    _to_ohlc(c),
                    bootstrap=True,
                )

            await client.subscribe_candles(
                monitor.primary_symbol,
                HTF_GRANULARITY,
            )

            # ====================================================
            # PRIMARY LTF HISTORY
            #
            # BOOTSTRAP = TRUE
            #
            # HAKUNA SIGNAL / PENDING SETUP
            # KUTOKA HISTORY.
            # ====================================================

            ltf_hist = (
                await client.get_candle_history(
                    monitor.primary_symbol,
                    LTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )

            for c in ltf_hist:

                cc = _to_ohlc(c)

                monitor.ltf_closes.append(
                    cc["close"]
                )

                monitor.ltf.add_candle(
                    cc,
                    bootstrap=True,
                )

            await client.subscribe_candles(
                monitor.primary_symbol,
                LTF_GRANULARITY,
            )

            # ====================================================
            # SECONDARY SMT HISTORY
            #
            # BOOTSTRAP = TRUE
            # ====================================================

            sec_hist = (
                await client.get_candle_history(
                    monitor.secondary_symbol,
                    LTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )

            for c in sec_hist:

                monitor.ltf_secondary.add_candle(
                    _to_ohlc(c),
                    bootstrap=True,
                )

            await client.subscribe_candles(
                monitor.secondary_symbol,
                LTF_GRANULARITY,
            )

            # ====================================================
            # STARTUP COMPLETE
            # ====================================================

            log.info(
                "[%s] STARTUP COMPLETE -> "
                "Historical context loaded. "
                "No historical signal/setup allowed. "
                "Waiting for LIVE candles.",
                monitor.display_name,
            )

            await client.wait_until_disconnected()

        except asyncio.CancelledError:

            raise

        except Exception as e:

            connected_duration = (
                time.time()
                - started_at
            )

            if (
                connected_duration
                > 120
            ):

                backoff = 5

            log.error(
                "[%s] Connection lost: %s",
                monitor.display_name,
                e,
            )

            try:

                await client.close()

            except Exception:

                pass

            await asyncio.sleep(
                backoff
            )

            backoff = min(
                backoff * 2,
                max_backoff,
            )


# ============================================================
# PROCESS LOCK
# ============================================================

async def acquire_process_lock():

    """
    Hii SI signal lock.

    Inazuia bot instances mbili za BOT nzima
    ku-run kwenye Linux host moja.
    """

    try:

        import fcntl

    except ImportError:

        log.warning(
            "fcntl haipo; process lock "
            "haijawezeshwa."
        )

        return None

    path = os.getenv(
        "SIGNAL_BOT_LOCK_FILE",
        "/tmp/deriv_signal_bot.lock",
    )

    handle = open(
        path,
        "w",
    )

    try:

        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX
            | fcntl.LOCK_NB,
        )

    except BlockingIOError:

        handle.close()

        raise RuntimeError(
            "Signal bot tayari ina-run "
            "kwenye host hii."
        )

    return handle


# ============================================================
# MAIN
# ============================================================

async def main():

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        raise SystemExit(
            "Weka TELEGRAM_BOT_TOKEN "
            "na TELEGRAM_CHAT_ID kwenye Variables."
        )

    process_lock = (
        await acquire_process_lock()
    )

    try:

        telegram = (
            TelegramNotifier(
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_CHAT_ID,
            )
        )

        signal_tracker = (
            SignalTracker()
        )

        names = ", ".join(
            p[2]
            for p in SYMBOL_PAIRS
        )

        await telegram.send(
            "🤖 <b>Signal Bot v5 imeanza</b>\n"
            f"Symbols: {names}\n"
            f"HTF: {HTF_GRANULARITY}s | "
            f"LTF: {LTF_GRANULARITY}s\n\n"

            "⭐ <b>QUALITY MODE: ON</b>\n"
            "Liquidity Sweep ✓\n"
            "Displacement ✓\n"
            "CHoCH ✓\n"
            "Valid OB ✓\n"
            "OB Retest ✓\n"
            "HTF Bias ✓\n"
            "RSI/SMA ✓\n"
            "Fresh SMT ✓\n\n"

            "🔓 <b>GLOBAL SIGNAL LOCK: OFF</b>\n"
            "Signals nyingi zinaweza kuwa ACTIVE.\n"
            "Kila signal ina TP/SL tracking yake.\n\n"

            "🕯️ <b>CANDLE DEDUPE: ACTIVE</b>\n"
            "Candle moja haiwezi kutuma signal "
            "zaidi ya moja.\n\n"

            "🚀 <b>STARTUP BOOTSTRAP: ACTIVE</b>\n"
            "Historical candles hazitatengeneza "
            "signal au pending setup.\n\n"

            "⚠️ Hii HAITRADE."
        )

        monitors = [
            PairMonitor(
                primary,
                secondary,
                display,
                telegram,
                signal_tracker,
            )
            for (
                primary,
                secondary,
                display,
            ) in SYMBOL_PAIRS
        ]

        await asyncio.gather(
            *(
                run_pair(
                    monitor
                )
                for monitor in monitors
            )
        )

    finally:

        if (
            process_lock
            is not None
        ):

            try:

                import fcntl

                fcntl.flock(
                    process_lock.fileno(),
                    fcntl.LOCK_UN,
                )

            except Exception:

                pass

            process_lock.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except RuntimeError as exc:

        log.error(
            "%s",
            exc,
        )

        raise SystemExit(1)
