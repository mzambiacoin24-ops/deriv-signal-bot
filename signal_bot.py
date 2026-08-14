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
        ] = float(
            value.strip()
        )

    except ValueError:
        pass


# ============================================================
# SYMBOLS
#
# 2s:
# R_10
# R_25
# R_50
# R_75
# R_100
#
# 1s:
# 1HZ10V
# 1HZ25V
# 1HZ50V
# 1HZ75V
# 1HZ100V
# ============================================================

SYMBOLS = [

    (
        "R_10",
        "Volatility 10 Index",
        "2s",
        "R_10",
    ),

    (
        "1HZ10V",
        "Volatility 10 (1s) Index",
        "1s",
        "R_10",
    ),

    (
        "R_25",
        "Volatility 25 Index",
        "2s",
        "R_25",
    ),

    (
        "1HZ25V",
        "Volatility 25 (1s) Index",
        "1s",
        "R_25",
    ),

    (
        "R_50",
        "Volatility 50 Index",
        "2s",
        "R_50",
    ),

    (
        "1HZ50V",
        "Volatility 50 (1s) Index",
        "1s",
        "R_50",
    ),

    (
        "R_75",
        "Volatility 75 Index",
        "2s",
        "R_75",
    ),

    (
        "1HZ75V",
        "Volatility 75 (1s) Index",
        "1s",
        "R_75",
    ),

    (
        "R_100",
        "Volatility 100 Index",
        "2s",
        "R_100",
    ),

    (
        "1HZ100V",
        "Volatility 100 (1s) Index",
        "1s",
        "R_100",
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


# ============================================================
# SL / TP
# ============================================================

def calculate_levels(
    direction,
    entry,
    structure,
):

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

    ranges = []

    for candle in candles[-20:]:

        value = abs(
            candle["high"]
            - candle["low"]
        )

        if value > 0:
            ranges.append(value)

    if not ranges:
        return None, None

    avg_range = (
        sum(ranges)
        / len(ranges)
    )

    # ========================================================
    # BUY
    # ========================================================

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
                support
                - (
                    avg_range
                    * 0.20
                ),

                entry
                - (
                    avg_range
                    * 0.80
                ),
            )

        else:

            sl = (
                entry
                - avg_range
            )

        risk = (
            entry
            - sl
        )

        if risk <= 0:
            return None, None

        usable_highs = [
            x
            for x in highs
            if x > entry
        ]

        if usable_highs:

            resistance = min(
                usable_highs[-5:]
            )

            minimum_tp = (
                entry
                + (
                    risk
                    * MIN_RR_RATIO
                )
            )

            tp = max(
                resistance,
                minimum_tp,
            )

        else:

            tp = (
                entry
                + (
                    risk
                    * MIN_RR_RATIO
                )
            )

        return sl, tp

    # ========================================================
    # SELL
    # ========================================================

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
                avg_range
                * 0.20
            ),

            entry
            + (
                avg_range
                * 0.80
            ),
        )

    else:

        sl = (
            entry
            + avg_range
        )

    risk = (
        sl
        - entry
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

        minimum_tp = (
            entry
            - (
                risk
                * MIN_RR_RATIO
            )
        )

        tp = min(
            support,
            minimum_tp,
        )

    else:

        tp = (
            entry
            - (
                risk
                * MIN_RR_RATIO
            )
        )

    return sl, tp


# ============================================================
# TIMEFRAME BUILDER
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

                "epoch":
                    bucket,

                "open":
                    candle["open"],

                "high":
                    candle["high"],

                "low":
                    candle["low"],

                "close":
                    candle["close"],

                "granularity":
                    self.seconds,
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

            "epoch":
                bucket,

            "open":
                candle["open"],

            "high":
                candle["high"],

            "low":
                candle["low"],

            "close":
                candle["close"],

            "granularity":
                self.seconds,
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
        feed_label,
        point_symbol,
        telegram,
    ):

        self.symbol = symbol

        self.display_name = (
            display_name
        )

        self.feed_label = (
            feed_label
        )

        self.point_symbol = (
            point_symbol
        )

        self.telegram = telegram

        # ----------------------------------------------------
        # M15
        # ----------------------------------------------------

        self.htf = SMCAnalyzer(
            symbol,
            lookback=2,
            history=300,
        )

        # ----------------------------------------------------
        # M5
        # ----------------------------------------------------

        self.mtf = SMCAnalyzer(
            symbol,
            lookback=2,
            history=300,
        )

        # ----------------------------------------------------
        # M1
        # ----------------------------------------------------

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
                point_symbol
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
            "[%s | %s] Inapakua history...",
            self.display_name,
            self.feed_label,
        )

        try:

            htf_data = (
                await client.get_candles(
                    self.symbol,
                    granularity=900,
                    count=CANDLE_COUNT,
                )
            )

            mtf_data = (
                await client.get_candles(
                    self.symbol,
                    granularity=300,
                    count=CANDLE_COUNT,
                )
            )

            ltf_data = (
                await client.get_candles(
                    self.symbol,
                    granularity=60,
                    count=CANDLE_COUNT,
                )
            )

            # ------------------------------------------------
            # M15
            # ------------------------------------------------

            for candle in htf_data:

                self.htf.add_candle(
                    clean_candle(
                        candle
                    )
                )

            # ------------------------------------------------
            # M5
            # ------------------------------------------------

            for candle in mtf_data:

                self.mtf.add_candle(
                    clean_candle(
                        candle
                    )
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
            # BUILDERS
            # ------------------------------------------------

            if mtf_data:

                self.mtf_builder.current = (
                    clean_candle(
                        mtf_data[-1]
                    )
                )

            if htf_data:

                self.htf_builder.current = (
                    clean_candle(
                        htf_data[-1]
                    )
                )

            self.ready = True

            log.info(
                "[%s | %s] History imekamilika | "
                "M15=%s | M5=%s | M1=%s | "
                "HTF=%s | MTF=%s | LTF=%s",

                self.display_name,

                self.feed_label,

                len(htf_data),

                len(mtf_data),

                len(ltf_data),

                self.htf.trend,

                self.mtf.trend,

                self.ltf.trend,
            )

        except Exception as exc:

            log.exception(
                "[%s | %s] History error: %s",
                self.display_name,
                self.feed_label,
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

        completed_m1 = (
            self.ltf_builder_update(
                c
            )
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
        # SIGNAL
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
        # M15
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
        # M5
        # ----------------------------------------------------

        mtf_direction = (
            self.mtf.trend
        )

        if (
            mtf_direction
            != htf_direction
        ):
            return

        # ----------------------------------------------------
        # M1
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
        # STRUCTURE
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
        # PRICE
        # ----------------------------------------------------

        price = float(
            candle["close"]
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        rsi_value = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        # ----------------------------------------------------
        # SMA
        # ----------------------------------------------------

        sma_value = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        # ----------------------------------------------------
        # RSI CONFLUENCE
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
        # SMA CONFLUENCE
        # ----------------------------------------------------

        sma_ok = True

        if sma_value is not None:

            if htf_direction == "up":

                sma_ok = (
                    price
                    >= sma_value
                )

            else:

                sma_ok = (
                    price
                    <= sma_value
                )

        # ----------------------------------------------------
        # CONFLUENCE
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

        if (
            sl is None
            or tp is None
        ):
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
        # DUPLICATE DIRECTION
        # ----------------------------------------------------

        if (
            self.last_signal_direction
            == htf_direction
            and (
                now
                - self.last_signal_time
                < (
                    MIN_SECONDS_BETWEEN_SIGNALS
                    * 2
                )
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
        # LOT
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
                round(
                    lot,
                    2,
                ),
                0.01,
            )

        # ----------------------------------------------------
        # ACTION
        # ----------------------------------------------------

        if htf_direction == "up":

            action = "NUNUA (BUY)"

            icon = "📈"

        else:

            action = "UZA (SELL)"

            icon = "📉"

        # ----------------------------------------------------
        # RSI TEXT
        # ----------------------------------------------------

        if rsi_value is None:

            rsi_text = "N/A"

        else:

            rsi_text = (
                f"{rsi_value:.1f}"
            )

        # ----------------------------------------------------
        # SMA TEXT
        # ----------------------------------------------------

        if sma_value is None:

            sma_text = "N/A"

        elif price >= sma_value:

            sma_text = "JUU"

        else:

            sma_text = "CHINI"

        # ====================================================
        # LIQUIDITY SWEEP
        #
        # HAPA NDIPO PEKEE TUMEBORESHA UFAFANUZI.
        # SIGNAL LOGIC HAJABADILIKA.
        # ====================================================

        sweep = (
            ltf_setup.get(
                "sweep"
            )
        )

        if sweep == "high":

            sweep_text = (
                "BSL SWEPT — "
                "Buy-side liquidity taken "
                "(bei imevuka swing high "
                "na kufunga chini yake)"
            )

        elif sweep == "low":

            sweep_text = (
                "SSL SWEPT — "
                "Sell-side liquidity taken "
                "(bei imevuka swing low "
                "na kufunga juu yake)"
            )

        else:

            sweep_text = "HAIPO"

        # ----------------------------------------------------
        # LOT TEXT
        # ----------------------------------------------------

        lot_text = (
            f"{lot}"
            if lot is not None
            else "N/A"
        )

        # ----------------------------------------------------
        # FEED NAME
        # ----------------------------------------------------

        if self.feed_label == "1s":

            feed_name = (
                "1 SECOND (1s)"
            )

        else:

            feed_name = (
                "2 SECONDS (2s)"
            )

        # ====================================================
        # TELEGRAM MESSAGE
        # ====================================================

        message = (

            f"{icon} "
            f"<b>ISHARA: "
            f"{action}</b>\n\n"

            f"📡 <b>FEED: "
            f"{feed_name}</b>\n"

            f"📌 Deriv Symbol: "
            f"<b>{self.symbol}</b>\n"

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

        # ====================================================
        # SEND
        # ====================================================

        try:

            await self.telegram.send(
                message
            )

            self.last_signal_time = (
                now
            )

            self.last_signal_direction = (
                htf_direction
            )

            log.info(
                "[%s | %s] SIGNAL %s | "
                "entry=%.4f sl=%.4f "
                "tp=%.4f RR=%.2f",

                self.display_name,

                self.feed_label,

                action,

                price,

                sl,

                tp,

                rr,
            )

        except Exception as exc:

            log.exception(
                "[%s | %s] Telegram error: %s",

                self.display_name,

                self.feed_label,

                exc,
            )


# ============================================================
# MAIN
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

    # ========================================================
    # CREATE 10 MONITORS
    # ========================================================

    for (
        deriv_symbol,
        display_name,
        feed_label,
        point_symbol,
    ) in SYMBOLS:

        monitor = PairMonitor(

            deriv_symbol,

            display_name,

            feed_label,

            point_symbol,

            telegram,
        )

        monitors.append(
            monitor
        )

    # ========================================================
    # LOAD HISTORY
    # ========================================================

    for monitor in monitors:

        await monitor.initialize(
            client
        )

    # ========================================================
    # CALLBACK
    # ========================================================

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

    # ========================================================
    # START ALL 10 STREAMS
    # ========================================================

    for monitor in monitors:

        try:

            await client.subscribe_candles(
                monitor.symbol,
                granularity=60,
            )

            log.info(
                "[%s | %s | %s] "
                "Live M1 stream started.",

                monitor.display_name,

                monitor.feed_label,

                monitor.symbol,
            )

            await asyncio.sleep(
                0.5
            )

        except Exception as exc:

            log.exception(
                "[%s | %s] "
                "Stream start error: %s",

                monitor.display_name,

                monitor.feed_label,

                exc,
            )

    # ========================================================
    # TELEGRAM START MESSAGE
    # ========================================================

    try:

        await telegram.send(

            "🤖 <b>Signal Bot v6</b>\n\n"

            "Bot imeanza kuchambua "
            "<b>feeds zote mbili</b>.\n\n"

            "⚡ <b>1s feeds:</b>\n"
            "• Volatility 10\n"
            "• Volatility 25\n"
            "• Volatility 50\n"
            "• Volatility 75\n"
            "• Volatility 100\n\n"

            "⏱️ <b>2s feeds:</b>\n"
            "• Volatility 10\n"
            "• Volatility 25\n"
            "• Volatility 50\n"
            "• Volatility 75\n"
            "• Volatility 100\n\n"

            "Kila signal itaonyesha wazi:\n"

            "📡 FEED: "
            "<b>1 SECOND (1s)</b> "
            "au "
            "<b>2 SECONDS (2s)</b>\n"

            "📌 Deriv Symbol\n"

            "📍 Symbol ya MT5\n\n"

            "M15 = HTF direction\n"
            "M5 = confirmation\n"
            "M1 = entry\n\n"

            "⚠️ Analysis only."
        )

    except Exception as exc:

        log.error(
            "Telegram startup message failed: %s",
            exc,
        )

    # ========================================================
    # KEEP RUNNING
    # ========================================================

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
