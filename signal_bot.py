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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("signal-bot")


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


HTF_GRANULARITY = int(
    os.getenv("SIGNAL_HTF_GRANULARITY", "900")
)

LTF_GRANULARITY = int(
    os.getenv("SIGNAL_LTF_GRANULARITY", "60")
)

CANDLE_COUNT = int(
    os.getenv("SIGNAL_CANDLE_COUNT", "200")
)


RSI_PERIOD = int(
    os.getenv("SIGNAL_RSI_PERIOD", "14")
)

RSI_OVERBOUGHT = float(
    os.getenv("SIGNAL_RSI_OVERBOUGHT", "70")
)

RSI_OVERSOLD = float(
    os.getenv("SIGNAL_RSI_OVERSOLD", "30")
)

SMA_TREND = int(
    os.getenv("SIGNAL_SMA_TREND", "50")
)


# Reduced from 1.5 to 1.3
MIN_RR_RATIO = float(
    os.getenv("MIN_RR_RATIO", "1.3")
)

# Reduced from 1800 seconds to 900 seconds
MIN_SECONDS_BETWEEN_SIGNALS = int(
    os.getenv(
        "MIN_SECONDS_BETWEEN_SIGNALS",
        "900",
    )
)

SR_BUFFER_PCT = float(
    os.getenv("SR_BUFFER_PCT", "0.15")
)


ACCOUNT_BALANCE = float(
    os.getenv("ACCOUNT_BALANCE", "10000")
)

RISK_PERCENT_PER_TRADE = float(
    os.getenv("RISK_PERCENT_PER_TRADE", "1")
)


POINT_VALUES = {}

for _pair in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1",
).split(","):

    if "=" in _pair:
        _sym, _val = _pair.split("=", 1)

        try:
            POINT_VALUES[
                _sym.strip()
            ] = float(_val.strip())

        except ValueError:
            pass


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


def _to_ohlc(c):
    return {
        "open": float(c["open"]),
        "high": float(c["high"]),
        "low": float(c["low"]),
        "close": float(c["close"]),
        "epoch": c.get("epoch"),
    }


def compute_sr_sl_tp(
    direction,
    entry_price,
    swing_highs,
    swing_lows,
    buffer_pct,
):

    buffer = (
        entry_price
        * (buffer_pct / 100)
    )

    supports = [
        level
        for level in swing_lows
        if level <= entry_price
    ]

    resistances = [
        level
        for level in swing_highs
        if level >= entry_price
    ]

    if not supports or not resistances:
        return None, None

    nearest_support = max(supports)
    nearest_resistance = min(resistances)

    if direction == "up":

        sl = (
            nearest_support
            - buffer
        )

        tp = (
            nearest_resistance
            - buffer
        )

        if not (
            sl < entry_price < tp
        ):
            return None, None

    else:

        sl = (
            nearest_resistance
            + buffer
        )

        tp = (
            nearest_support
            + buffer
        )

        if not (
            tp < entry_price < sl
        ):
            return None, None

    return sl, tp


class PairMonitor:

    def __init__(
        self,
        primary_symbol,
        secondary_symbol,
        display_name,
        telegram,
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

        self.htf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf_secondary = SMCAnalyzer(
            secondary_symbol
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

        self.last_signal_time = 0


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

        c = _to_ohlc(ohlc)


        if (
            symbol
            == self.primary_symbol
            and granularity
            == HTF_GRANULARITY
        ):

            self.htf.add_candle(c)


        elif (
            symbol
            == self.primary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            self.ltf_closes.append(
                c["close"]
            )

            entry = (
                self.ltf.add_candle(c)
            )

            if entry:
                await self._maybe_send_signal(
                    entry,
                    c["close"],
                )


        elif (
            symbol
            == self.secondary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            self.ltf_secondary.add_candle(
                c
            )


    async def _maybe_send_signal(
        self,
        entry,
        price,
    ):

        direction = entry[
            "direction"
        ]

        fvg = entry[
            "fvg"
        ]


        # --------------------------------------------------
        # 1. HTF MARKET STRUCTURE
        # --------------------------------------------------

        if self.htf.trend is None:

            log.info(
                "[%s] Imekataliwa: HTF trend bado haijathibitishwa.",
                self.display_name,
            )

            return


        if (
            self.htf.trend
            != direction
        ):

            log.info(
                "[%s] Imekataliwa: direction ya LTF (%s) haiendani na HTF (%s).",
                self.display_name,
                direction,
                self.htf.trend,
            )

            return


        # --------------------------------------------------
        # 2. FVG
        # --------------------------------------------------

        if fvg is None:

            log.info(
                "[%s] Imekataliwa: hakuna FVG kwenye setup.",
                self.display_name,
            )

            return


        # --------------------------------------------------
        # 3. RSI / SMA
        #
        # HAZIKATALI SIGNAL.
        # Zinatumika kama confluence.
        # --------------------------------------------------

        rsi_val = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        sma_val = sma(
            self.ltf_closes,
            SMA_TREND,
        )


        rsi_ok = True
        sma_ok = True


        if rsi_val is not None:

            if direction == "up":

                rsi_ok = (
                    rsi_val
                    < RSI_OVERBOUGHT
                )

            else:

                rsi_ok = (
                    rsi_val
                    > RSI_OVERSOLD
                )


        if sma_val is not None:

            if direction == "up":

                sma_ok = (
                    price >= sma_val
                )

            else:

                sma_ok = (
                    price <= sma_val
                )


        # --------------------------------------------------
        # 4. SMT
        #
        # SMT SI LAZIMA.
        # Ni confluence tu.
        # --------------------------------------------------

        primary_swept = (
            self.ltf.last_sweep
        )

        secondary_swept = (
            self.ltf_secondary.last_sweep
        )


        smt_confirmed = (
            primary_swept is not None
            and secondary_swept is not None
            and primary_swept
            != secondary_swept
        )


        # --------------------------------------------------
        # 5. COOLDOWN
        # --------------------------------------------------

        now = time.time()

        if (
            now
            - self.last_signal_time
            < MIN_SECONDS_BETWEEN_SIGNALS
        ):

            remaining = int(
                MIN_SECONDS_BETWEEN_SIGNALS
                - (
                    now
                    - self.last_signal_time
                )
            )

            log.info(
                "[%s] Bado kwenye cooldown (%ds zimebaki).",
                self.display_name,
                remaining,
            )

            return


        # --------------------------------------------------
        # 6. SUPPORT / RESISTANCE
        # --------------------------------------------------

        sl_price, tp_price = (
            compute_sr_sl_tp(
                direction,
                price,
                self.htf.swing_highs,
                self.htf.swing_lows,
                SR_BUFFER_PCT,
            )
        )


        if (
            sl_price is None
            or tp_price is None
        ):

            log.info(
                "[%s] Imekataliwa: hakuna S/R ya kutosha kwa SL/TP.",
                self.display_name,
            )

            return


        sl_distance = abs(
            price - sl_price
        )

        tp_distance = abs(
            tp_price - price
        )


        if sl_distance <= 0:
            return


        rr = (
            tp_distance
            / sl_distance
        )


        # --------------------------------------------------
        # 7. R:R
        # --------------------------------------------------

        if rr < MIN_RR_RATIO:

            log.info(
                "[%s] Imekataliwa: R:R %.2f iko chini ya 1:%.2f.",
                self.display_name,
                rr,
                MIN_RR_RATIO,
            )

            return


        # --------------------------------------------------
        # 8. SIGNAL CONFIDENCE
        # --------------------------------------------------

        confidence_points = 0

        if rsi_val is not None and rsi_ok:
            confidence_points += 1

        if sma_val is not None and sma_ok:
            confidence_points += 1

        if smt_confirmed:
            confidence_points += 1


        if confidence_points >= 3:

            confidence = "HIGH"

        elif confidence_points == 2:

            confidence = "GOOD"

        else:

            confidence = "STANDARD"


        # --------------------------------------------------
        # 9. SEND SIGNAL
        # --------------------------------------------------

        self.last_signal_time = now


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
                round(lot, 2),
                0.01,
            )

            lot_line = (
                f"📊 Lot Size (pendekezo): "
                f"<b>{lot}</b>\n"
            )

        else:

            lot_line = (
                "📊 Lot Size: weka POINT_VALUES "
                "ya symbol hii kwenye .env "
                "(MT5 → Specification)\n"
            )


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


        rsi_text = (
            f"{rsi_val:.1f}"
            if rsi_val is not None
            else "N/A"
        )


        sma_text = (
            "juu"
            if (
                sma_val is not None
                and price > sma_val
            )
            else "chini"
        )


        smt_text = (
            "IMETHIBITIKA"
            if smt_confirmed
            else "HAIPO"
        )


        await self.telegram.send(

            f"{emoji} "
            f"<b>ISHARA: {action}</b>\n"

            f"Symbol (MT5): "
            f"<b>{self.display_name}</b>\n"

            f"🎯 Confidence: "
            f"<b>{confidence}</b>\n"

            f"💰 Bei ya kuingia: "
            f"{price:.4f}\n"

            f"🎯 Take Profit: "
            f"{tp_price:.4f} "
            f"({tp_distance:.4f})\n"

            f"🛑 Stop Loss: "
            f"{sl_price:.4f} "
            f"({sl_distance:.4f})\n"

            f"⚖️ R:R = "
            f"1:{rr:.2f}\n"

            f"{lot_line}"

            f"\n"
            f"📐 Market Structure: "
            f"<b>{self.htf.trend.upper()}</b>\n"

            f"🧠 HTF(15m) confirmed structure\n"

            f"🔄 LTF(1m): "
            f"CHoCH + FVG + OB retest\n"

            f"📊 RSI(14): "
            f"{rsi_text}\n"

            f"📏 Bei dhidi ya SMA{SMA_TREND}: "
            f"{sma_text}\n"

            f"🔗 SMT: "
            f"<b>{smt_text}</b>\n"

            f"📌 RSI/SMA/SMT zimetumika "
            f"kama confluence, si filters mandatory.\n"

            f"\n"
            f"⚠️ Hii ni PENDEKEZO TU "
            f"(si ushauri wa kifedha) - "
            f"fanya uamuzi wako mwenyewe "
            f"kabla ya kubonyeza kwenye MT5."
        )


        log.info(
            "[%s] ISHARA %s imetumwa | "
            "Entry %.4f | TP %.4f | SL %.4f | "
            "R:R 1:%.2f | Confidence=%s | "
            "SMT=%s | RSI_OK=%s | SMA_OK=%s",
            self.display_name,
            direction.upper(),
            price,
            tp_price,
            sl_price,
            rr,
            confidence,
            smt_confirmed,
            rsi_ok,
            sma_ok,
        )


async def run_pair(monitor):

    client = PublicMarketClient()

    client.on_candle = (
        monitor.on_candle
    )

    backoff = 5
    max_backoff = 300


    while True:

        started_at = time.time()

        try:

            await client.connect()

            log.info(
                "[%s] Umeunganishwa (public data).",
                monitor.display_name,
            )


            # -------------------------
            # HTF HISTORY
            # -------------------------

            htf_hist = (
                await client.get_candle_history(
                    monitor.primary_symbol,
                    HTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )


            for c in htf_hist:

                monitor.htf.add_candle(
                    _to_ohlc(c)
                )


            await client.subscribe_candles(
                monitor.primary_symbol,
                HTF_GRANULARITY,
            )


            # -------------------------
            # LTF HISTORY
            # -------------------------

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
                    cc
                )


            await client.subscribe_candles(
                monitor.primary_symbol,
                LTF_GRANULARITY,
            )


            # -------------------------
            # SECONDARY SYMBOL
            # -------------------------

            sec_hist = (
                await client.get_candle_history(
                    monitor.secondary_symbol,
                    LTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )


            for c in sec_hist:

                monitor.ltf_secondary.add_candle(
                    _to_ohlc(c)
                )


            await client.subscribe_candles(
                monitor.secondary_symbol,
                LTF_GRANULARITY,
            )


            log.info(
                "[%s] Historia imepakiwa "
                "(HTF+LTF+pacha), "
                "inasubiri candles mpya...",
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


            if connected_duration > 120:
                backoff = 5


            log.error(
                "[%s] Muunganiko umekatika: %s",
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


async def main():

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        raise SystemExit(
            "Weka TELEGRAM_BOT_TOKEN "
            "na TELEGRAM_CHAT_ID kwenye .env."
        )


    telegram = TelegramNotifier(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )


    names = ", ".join(
        pair[2]
        for pair in SYMBOL_PAIRS
    )


    await telegram.send(

        f"🤖 <b>Signal Bot v4 imeanza</b>\n"

        f"Symbols: {names}\n"

        f"HTF bias: "
        f"{HTF_GRANULARITY}s | "
        f"LTF entry: "
        f"{LTF_GRANULARITY}s\n"

        f"Market Structure: "
        f"HH/HL + LH/LL confirmation\n"

        f"Setup: "
        f"HTF + CHoCH + FVG + OB retest\n"

        f"R:R minimum: "
        f"1:{MIN_RR_RATIO}\n"

        f"Cooldown: "
        f"{MIN_SECONDS_BETWEEN_SIGNALS}s\n"

        f"RSI/SMA/SMT: "
        f"Confluence, si mandatory\n"

        f"HAKUNA kikomo cha idadi ya "
        f"ishara kwa siku.\n\n"

        f"⚠️ Hii HAITRADE - "
        f"inatuma mapendekezo TU."
    )


    monitors = [

        PairMonitor(
            primary,
            secondary,
            display,
            telegram,
        )

        for primary, secondary, display
        in SYMBOL_PAIRS
    ]


    try:

        await asyncio.gather(
            *(
                run_pair(monitor)
                for monitor in monitors
            )
        )


    except KeyboardInterrupt:

        await telegram.send(
            "🛑 Signal bot imesimamishwa "
            "na mtumiaji."
        )


if __name__ == "__main__":

    backoff = 5
    max_backoff = 300


    while True:

        started_at = time.time()

        try:

            asyncio.run(main())

            break


        except KeyboardInterrupt:

            log.info(
                "Imesimamishwa na mtumiaji."
            )

            break


        except Exception as e:

            connected_duration = (
                time.time()
                - started_at
            )


            if connected_duration > 300:
                backoff = 5


            log.error(
                "Bot imeanguka: %s. "
                "Inaanza upya baada ya %ds...",
                e,
                backoff,
            )


            time.sleep(backoff)


            backoff = min(
                backoff * 2,
                max_backoff,
        )
