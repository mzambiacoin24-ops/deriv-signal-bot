import asyncio
import logging
import os
import time
from collections import deque

from dotenv import load_dotenv

from public_client import PublicMarketClient
from smc import SMCAnalyzer
from indicators import sma, rsi
from telegram_notifier import TelegramNotifier


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
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()


# ============================================================
# SETTINGS
# ============================================================

CANDLE_COUNT = int(
    os.getenv(
        "SIGNAL_CANDLE_COUNT",
        "200",
    )
)

RSI_PERIOD = int(
    os.getenv(
        "SIGNAL_RSI_PERIOD",
        "14",
    )
)

SMA_TREND = int(
    os.getenv(
        "SIGNAL_SMA_TREND",
        "50",
    )
)

MIN_RR_RATIO = float(
    os.getenv(
        "MIN_RR_RATIO",
        "1.30",
    )
)

MIN_SECONDS_BETWEEN_SIGNALS = int(
    os.getenv(
        "MIN_SECONDS_BETWEEN_SIGNALS",
        "900",
    )
)

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


# ============================================================
# POINT VALUES
# ============================================================

POINT_VALUES = {}

for item in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1",
).split(","):

    if "=" not in item:
        continue

    symbol, value = item.split(
        "=",
        1,
    )

    try:
        POINT_VALUES[
            symbol.strip()
        ] = float(value.strip())

    except ValueError:
        pass


# ============================================================
# SYMBOLS
# ============================================================

SYMBOLS = [
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
# HELPERS
# ============================================================

def clean_candle(candle):
    return {
        "epoch": int(
            candle["epoch"]
        ),
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
        "granularity": int(
            candle.get(
                "granularity",
                60,
            )
        ),
    }


def direction_text(direction):
    if direction == "up":
        return "BUY"

    return "SELL"


def calculate_levels(
    direction,
    entry,
    structure,
):
    """
    SL/TP mpya.

    Haitaki lazima resistance na support zote
    ziwepo karibu sana.

    Inatumia recent structure na ATR-like
    movement ya candles.
    """

    highs = list(
        structure.swing_highs
    )

    lows = list(
        structure.swing_lows
    )

    candles = list(
        structure.candles
    )

    if len(candles) < 10:
        return None, None

    recent = candles[-20:]

    ranges = []

    for c in recent:
        ranges.append(
            abs(
                c["high"]
                - c["low"]
            )
        )

    ranges = [
        x for x in ranges
        if x > 0
    ]

    if not ranges:
        return None, None

    avg_range = (
        sum(ranges)
        / len(ranges)
    )

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if direction == "up":

        usable_lows = [
            x
            for x in lows
            if x < entry
        ]

        if usable_lows:

            support = max(
                usable_lows[-5:]
            )

            sl = min(
                support - (
                    avg_range * 0.20
                ),
                entry - (
                    avg_range * 0.80
                ),
            )

        else:

            sl = (
                entry
                - avg_range
            )

        risk = (
            entry - sl
        )

        if risk <= 0:
            return None, None

        # TP ya kwanza inayowezekana
        usable_highs = [
            x
            for x in highs
            if x > entry
        ]

        if usable_highs:

            resistance = min(
                usable_highs[-5:]
            )

            structural_tp = (
                resistance
            )

            minimum_tp = (
                entry
                + risk
                * MIN_RR_RATIO
            )

            tp = max(
                structural_tp,
                minimum_tp,
            )

        else:

            tp = (
                entry
                + risk
                * MIN_RR_RATIO
            )

        return sl, tp

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    usable_highs = [
        x
        for x in highs
        if x > entry
    ]

    if usable_highs:

        resistance = min(
            usable_highs[-5:]
        )

        sl = max(
            resistance
            + (
                avg_range * 0.20
            ),
            entry
            + (
                avg_range * 0.80
            ),
        )

    else:

        sl = (
            entry
            + avg_range
        )

    risk = (
        sl - entry
    )

    if risk <= 0:
        return None, None

    usable_lows = [
        x
        for x in lows
        if x < entry
    ]

    if usable_lows:

        support = max(
            usable_lows[-5:]
        )

        structural_tp = (
            support
        )

        minimum_tp = (
            entry
            - risk
            * MIN_RR_RATIO
        )

        tp = min(
            structural_tp,
            minimum_tp,
        )

    else:

        tp = (
            entry
            - risk
            * MIN_RR_RATIO
        )

    return sl, tp


# ============================================================
# TIMEFRAME AGGREGATOR
# ============================================================

class TimeframeBuilder:

    def __init__(
        self,
        seconds,
    ):
        self.seconds = seconds
        self.current = None

    def update(
        self,
        candle,
    ):
        epoch = int(
            candle["epoch"]
        )

        bucket = (
            epoch
            - (
                epoch
                % self.seconds
            )
        )

        if self.current is None:

            self.current = {
                "epoch": bucket,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "granularity": self.seconds,
            }

            return None

        if (
            bucket
            == self.current["epoch"]
        ):

            self.current["high"] = max(
                self.current["high"],
                candle["high"],
            )

            self.current["low"] = min(
                self.current["low"],
                candle["low"],
            )

            self.current["close"] = (
                candle["close"]
            )

            return None

        completed = dict(
            self.current
        )

        self.current = {
            "epoch": bucket,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "granularity": self.seconds,
        }

        return completed


# ============================================================
# PAIR MONITOR
# ============================================================

class PairMonitor:

    def __init__(
        self,
        symbol,
        display_name,
        telegram,
    ):

        self.symbol = symbol

        self.display_name = (
            display_name
        )

        self.telegram = telegram

        # ----------------------------------------------------
        # THREE TIMEFRAMES
        # ----------------------------------------------------

        self.htf = SMCAnalyzer(
            symbol,
            lookback=2,
            history=300,
        )

        self.mtf = SMCAnalyzer(
            symbol,
            lookback=2,
            history=300,
        )

        self.ltf = SMCAnalyzer(
            symbol,
            lookback=2,
            history=300,
        )

        # ----------------------------------------------------
        # BUILDERS
        # ----------------------------------------------------

        self.mtf_builder = (
            TimeframeBuilder(300)
        )

        self.htf_builder = (
            TimeframeBuilder(900)
        )

        # ----------------------------------------------------
        # INDICATORS
        # ----------------------------------------------------

        self.ltf_closes = deque(
            maxlen=100
        )

        # ----------------------------------------------------
        # SIGNAL CONTROL
        # ----------------------------------------------------

        self.last_signal_time = 0

        self.last_signal_direction = None

        self.point_value = (
            POINT_VALUES.get(
                symbol
            )
        )

        self.ready = False

    # ========================================================
    # INITIAL HISTORY
    # ========================================================

    async def initialize(
        self,
        client,
    ):

        log.info(
            "[%s] Inapakua history...",
            self.display_name,
        )

        try:

            htf_data = await client.get_candles(
                self.symbol,
                granularity=900,
                count=CANDLE_COUNT,
            )

            mtf_data = await client.get_candles(
                self.symbol,
                granularity=300,
                count=CANDLE_COUNT,
            )

            ltf_data = await client.get_candles(
                self.symbol,
                granularity=60,
                count=CANDLE_COUNT,
            )

            # ------------------------------------------------
            # M15
            # ------------------------------------------------

            for candle in htf_data:

                self.htf.add_candle(
                    clean_candle(candle)
                )

            # ------------------------------------------------
            # M5
            # ------------------------------------------------

            for candle in mtf_data:

                self.mtf.add_candle(
                    clean_candle(candle)
                )

            # ------------------------------------------------
            # M1
            # ------------------------------------------------

            for candle in ltf_data:

                c = clean_candle(
                    candle
                )

                self.ltf_closes.append(
                    c["close"]
                )

                self.ltf.add_candle(
                    c
                )

            # ------------------------------------------------
            # Prepare live builders
            # ------------------------------------------------

            if mtf_data:

                last_m5 = clean_candle(
                    mtf_data[-1]
                )

                self.mtf_builder.current = (
                    dict(last_m5)
                )

            if htf_data:

                last_m15 = clean_candle(
                    htf_data[-1]
                )

                self.htf_builder.current = (
                    dict(last_m15)
                )

            self.ready = True

            log.info(
                "[%s] History imekamilika | "
                "M15=%s | M5=%s | M1=%s | "
                "HTF=%s | MTF=%s | LTF=%s",
                self.display_name,
                len(htf_data),
                len(mtf_data),
                len(ltf_data),
                self.htf.trend,
                self.mtf.trend,
                self.ltf.trend,
            )

        except Exception as exc:

            log.exception(
                "[%s] History error: %s",
                self.display_name,
                exc,
            )

    # ========================================================
    # LIVE CANDLE
    # ========================================================

    async def on_candle(
        self,
        symbol,
        candle,
    ):

        if symbol != self.symbol:
            return

        if not self.ready:
            return

        granularity = int(
            candle.get(
                "granularity",
                60,
            )
        )

        if granularity != 60:
            return

        c = clean_candle(
            candle
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Public client sends the current 1m candle repeatedly.
        # We only process a completed 1m candle when the next
        # candle starts.
        # ----------------------------------------------------

        completed_m1 = (
            self.ltf_builder_update(c)
        )

        if completed_m1 is None:
            return

        # ----------------------------------------------------
        # M1
        # ----------------------------------------------------

        self.ltf_closes.append(
            completed_m1["close"]
        )

        ltf_entry = (
            self.ltf.add_candle(
                completed_m1
            )
        )

        # ----------------------------------------------------
        # M5
        # ----------------------------------------------------

        completed_m5 = (
            self.mtf_builder.update(
                completed_m1
            )
        )

        if completed_m5:

            self.mtf.add_candle(
                completed_m5
            )

        # ----------------------------------------------------
        # M15
        # ----------------------------------------------------

        completed_m15 = (
            self.htf_builder.update(
                completed_m1
            )
        )

        if completed_m15:

            self.htf.add_candle(
                completed_m15
            )

        # ----------------------------------------------------
        # CHECK SIGNAL
        # ----------------------------------------------------

        if ltf_entry:

            await self.evaluate_signal(
                ltf_entry,
                completed_m1,
            )

    # ========================================================
    # M1 BUILDER
    # ========================================================

    def ltf_builder_update(
        self,
        candle,
    ):

        if not hasattr(
            self,
            "_ltf_builder",
        ):

            self._ltf_builder = (
                TimeframeBuilder(60)
            )

        return self._ltf_builder.update(
            candle
        )

    # ========================================================
    # SIGNAL ENGINE
    # ========================================================

    async def evaluate_signal(
        self,
        ltf_setup,
        candle,
    ):

        now = time.time()

        # ----------------------------------------------------
        # COOLDOWN
        # ----------------------------------------------------

        if (
            now
            - self.last_signal_time
            < MIN_SECONDS_BETWEEN_SIGNALS
        ):
            return

        # ----------------------------------------------------
        # M15 DIRECTION
        # ----------------------------------------------------

        htf_direction = (
            self.htf.trend
        )

        if htf_direction not in (
            "up",
            "down",
        ):
            return

        # ----------------------------------------------------
        # M5 DIRECTION
        # ----------------------------------------------------

        mtf_direction = (
            self.mtf.trend
        )

        if mtf_direction != htf_direction:
            return

        # ----------------------------------------------------
        # M1 DIRECTION
        # ----------------------------------------------------

        ltf_direction = (
            ltf_setup.get(
                "direction"
            )
        )

        if (
            ltf_direction
            != htf_direction
        ):
            return

        # ----------------------------------------------------
        # STRUCTURE STRENGTH
        # ----------------------------------------------------

        if (
            self.htf.structure_strength
            == "NEUTRAL"
        ):
            return

        if (
            self.mtf.structure_strength
            == "NEUTRAL"
        ):
            return

        # ----------------------------------------------------
        # INDICATORS
        # ----------------------------------------------------

        price = float(
            candle["close"]
        )

        rsi_value = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        sma_value = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        # ----------------------------------------------------
        # RSI IS CONFLUENCE ONLY
        # ----------------------------------------------------

        rsi_ok = True

        if rsi_value is not None:

            if htf_direction == "up":

                rsi_ok = (
                    rsi_value < 75
                )

            else:

                rsi_ok = (
                    rsi_value > 25
                )

        # ----------------------------------------------------
        # SMA IS CONFLUENCE ONLY
        # ----------------------------------------------------

        sma_ok = True

        if sma_value is not None:

            if htf_direction == "up":

                sma_ok = (
                    price >= sma_value
                )

            else:

                sma_ok = (
                    price <= sma_value
                )

        # ----------------------------------------------------
        # WE DO NOT REQUIRE RSI/SMA
        # ----------------------------------------------------

        confluence = 0

        if rsi_ok:
            confluence += 1

        if sma_ok:
            confluence += 1

        if (
            ltf_setup.get(
                "sweep"
            )
            is not None
        ):
            confluence += 1

        # ----------------------------------------------------
        # SL / TP
        # ----------------------------------------------------

        sl, tp = calculate_levels(
            htf_direction,
            price,
            self.htf,
        )

        if sl is None or tp is None:
            return

        risk = abs(
            price - sl
        )

        reward = abs(
            tp - price
        )

        if risk <= 0:
            return

        rr = (
            reward / risk
        )

        if rr < MIN_RR_RATIO:
            return

        # ----------------------------------------------------
        # PREVENT SAME DIRECTION DUPLICATES
        # ----------------------------------------------------

        if (
            self.last_signal_direction
            == htf_direction
            and (
                now
                - self.last_signal_time
                < MIN_SECONDS_BETWEEN_SIGNALS
                * 2
            )
        ):
            return

        # ----------------------------------------------------
        # CONFIDENCE
        # ----------------------------------------------------

        if (
            htf_direction == "up"
            and self.htf.structure_strength
            == "STRONG"
            and self.mtf.structure_strength
            == "STRONG"
        ):
            confidence = "HIGH"

        elif confluence >= 2:
            confidence = "GOOD"

        else:
            confidence = "STANDARD"

        # ----------------------------------------------------
        # LOT SIZE
        # ----------------------------------------------------

        lot = None

        if (
            self.point_value is not None
            and self.point_value > 0
        ):

            risk_money = (
                ACCOUNT_BALANCE
                * (
                    RISK_PERCENT_PER_TRADE
                    / 100
                )
            )

            lot = (
                risk_money
                / (
                    risk
                    * self.point_value
                )
            )

            lot = max(
                round(lot, 2),
                0.01,
            )

        # ----------------------------------------------------
        # TEXT
        # ----------------------------------------------------

        if htf_direction == "up":

            action = "NUNUA (BUY)"

            icon = "📈"

        else:

            action = "UZA (SELL)"

            icon = "📉"

        if rsi_value is None:

            rsi_text = "N/A"

        else:

            rsi_text = (
                f"{rsi_value:.1f}"
            )

        if sma_value is None:

            sma_text = "N/A"

        elif price >= sma_value:

            sma_text = "JUU"

        else:

            sma_text = "CHINI"

        sweep = (
            ltf_setup.get(
                "sweep"
            )
        )

        if sweep:

            sweep_text = (
                sweep.upper()
            )

        else:

            sweep_text = "HAIPO"

        lot_text = (
            f"{lot}"
            if lot is not None
            else "N/A"
        )

        # ----------------------------------------------------
        # MESSAGE
        # ----------------------------------------------------

        message = (
            f"{icon} <b>ISHARA: "
            f"{action}</b>\n\n"

            f"Symbol (MT5): "
            f"<b>{self.display_name}</b>\n"

            f"🎯 Confidence: "
            f"<b>{confidence}</b>\n"

            f"💰 Entry: "
            f"<b>{price:.4f}</b>\n"

            f"🎯 Take Profit: "
            f"<b>{tp:.4f}</b> "
            f"({reward:.4f})\n"

            f"🛑 Stop Loss: "
            f"<b>{sl:.4f}</b> "
            f"({risk:.4f})\n"

            f"⚖️ R:R: "
            f"<b>1:{rr:.2f}</b>\n"

            f"📊 Lot Size: "
            f"<b>{lot_text}</b>\n\n"

            f"📐 Market Structure: "
            f"<b>{htf_direction.upper()}</b>\n"

            f"🧠 M15: "
            f"<b>{self.htf.trend.upper()}</b> "
            f"({self.htf.structure_strength})\n"

            f"🔄 M5: "
            f"<b>{self.mtf.trend.upper()}</b> "
            f"({self.mtf.structure_strength})\n"

            f"⚡ M1: "
            f"<b>{ltf_direction.upper()}</b>\n"

            f"📊 RSI({RSI_PERIOD}): "
            f"{rsi_text}\n"

            f"📏 SMA{SMA_TREND}: "
            f"{sma_text}\n"

            f"💧 Liquidity Sweep: "
            f"{sweep_text}\n"

            f"🧩 Setup: "
            f"<b>{ltf_setup.get('reason', 'SMC')}</b>\n\n"

            f"⚠️ <i>Hii ni pendekezo la "
            f"analysis tu, si ushauri wa kifedha.</i>"
        )

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        try:

            await self.telegram.send(
                message
            )

            self.last_signal_time = now

            self.last_signal_direction = (
                htf_direction
            )

            log.info(
                "[%s] SIGNAL %s | "
                "entry=%.4f sl=%.4f "
                "tp=%.4f RR=%.2f",
                self.display_name,
                action,
                price,
                sl,
                tp,
                rr,
            )

        except Exception as exc:

            log.exception(
                "[%s] Telegram error: %s",
                self.display_name,
                exc,
            )


# ============================================================
# MAIN BOT
# ============================================================

async def main():

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN haijawekwa."
        )

    if not TELEGRAM_CHAT_ID:

        raise RuntimeError(
            "TELEGRAM_CHAT_ID haijawekwa."
        )

    telegram = TelegramNotifier(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )

    client = PublicMarketClient(
        timeout=20
    )

    await client.connect()

    monitors = []

    # --------------------------------------------------------
    # CREATE MONITORS
    # --------------------------------------------------------

    for (
        internal_symbol,
        deriv_symbol,
        display_name,
    ) in SYMBOLS:

        monitor = PairMonitor(
            deriv_symbol,
            display_name,
            telegram,
        )

        monitors.append(
            monitor
        )

    # --------------------------------------------------------
    # LOAD HISTORY
    # --------------------------------------------------------

    for monitor in monitors:

        await monitor.initialize(
            client
        )

    # --------------------------------------------------------
    # ONE LIVE STREAM PER INDEX
    #
    # IMPORTANT:
    # Tunasubscribe 1-minute ticks ONLY.
    #
    # M5 na M15 zinajengwa locally.
    # Hii inazuia AlreadySubscribed.
    # --------------------------------------------------------

    async def callback(
        symbol,
        candle,
    ):

        for monitor in monitors:

            if (
                monitor.symbol
                == symbol
            ):

                await monitor.on_candle(
                    symbol,
                    candle,
                )

                break

    client.on_candle = callback

    # --------------------------------------------------------
    # START STREAMS
    # --------------------------------------------------------

    for monitor in monitors:

        try:

            await client.subscribe_candles(
                monitor.symbol,
                granularity=60,
            )

            log.info(
                "[%s] Live M1 stream started.",
                monitor.display_name,
            )

            await asyncio.sleep(
                0.5
            )

        except Exception as exc:

            log.exception(
                "[%s] Stream start error: %s",
                monitor.display_name,
                exc,
            )

    # --------------------------------------------------------
    # START MESSAGE
    # --------------------------------------------------------

    try:

        await telegram.send(
            "🤖 <b>Signal Bot v5</b>\n\n"
            "Bot imeanza.\n"
            "M15 = HTF direction\n"
            "M5 = confirmation\n"
            "M1 = entry\n\n"
            "Indices 5 zinafuatiliwa:\n"
            "• Volatility 10\n"
            "• Volatility 25\n"
            "• Volatility 50\n"
            "• Volatility 75\n"
            "• Volatility 100\n\n"
            "⚠️ Analysis only."
        )

    except Exception as exc:

        log.error(
            "Telegram startup message failed: %s",
            exc,
        )

    # --------------------------------------------------------
    # KEEP RUNNING
    # --------------------------------------------------------

    try:

        while True:

            await asyncio.sleep(
                60
            )

    except asyncio.CancelledError:

        pass

    finally:

        await client.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        log.info(
            "Bot stopped."
        )

    except Exception as exc:

        log.exception(
            "Fatal error: %s",
            exc,
                )
