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

HTF_GRANULARITY = int(os.getenv("SIGNAL_HTF_GRANULARITY", "900"))
LTF_GRANULARITY = int(os.getenv("SIGNAL_LTF_GRANULARITY", "60"))
CANDLE_COUNT = int(os.getenv("SIGNAL_CANDLE_COUNT", "200"))

RSI_PERIOD = int(os.getenv("SIGNAL_RSI_PERIOD", "14"))
RSI_OVERBOUGHT = float(os.getenv("SIGNAL_RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = float(os.getenv("SIGNAL_RSI_OVERSOLD", "30"))
SMA_TREND = int(os.getenv("SIGNAL_SMA_TREND", "50"))

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PERCENT_PER_TRADE = float(os.getenv("RISK_PERCENT_PER_TRADE", "1"))
RR_RATIO = float(os.getenv("RR_RATIO", "2"))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.1"))

POINT_VALUES = {}
for _pair in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1"
).split(","):
    if "=" in _pair:
        _sym, _val = _pair.split("=", 1)
        try:
            POINT_VALUES[_sym.strip()] = float(_val.strip())
        except ValueError:
            pass

SYMBOL_PAIRS = [
    ("R_10", "1HZ10V", "Volatility 10 Index"),
    ("R_25", "1HZ25V", "Volatility 25 Index"),
    ("R_50", "1HZ50V", "Volatility 50 Index"),
    ("R_75", "1HZ75V", "Volatility 75 Index"),
    ("R_100", "1HZ100V", "Volatility 100 Index"),
]


def _to_ohlc(c):
    return {
        "open": float(c["open"]),
        "high": float(c["high"]),
        "low": float(c["low"]),
        "close": float(c["close"]),
        "epoch": c.get("epoch"),
    }


class PairMonitor:
    def __init__(
        self,
        primary_symbol,
        secondary_symbol,
        display_name,
        telegram,
    ):
        self.primary_symbol = primary_symbol
        self.secondary_symbol = secondary_symbol
        self.display_name = display_name
        self.telegram = telegram

        self.htf = SMCAnalyzer(primary_symbol)
        self.ltf = SMCAnalyzer(primary_symbol)
        self.ltf_secondary = SMCAnalyzer(secondary_symbol)

        self.ltf_closes = deque(
            maxlen=max(RSI_PERIOD, SMA_TREND) + 5
        )
        self.point_value = POINT_VALUES.get(primary_symbol)

    async def on_candle(self, symbol, ohlc):
        try:
            granularity = int(ohlc.get("granularity", 0))
        except (TypeError, ValueError):
            return

        c = _to_ohlc(ohlc)

        if (
            symbol == self.primary_symbol
            and granularity == HTF_GRANULARITY
        ):
            self.htf.add_candle(c)

        elif (
            symbol == self.primary_symbol
            and granularity == LTF_GRANULARITY
        ):
            self.ltf_closes.append(c["close"])
            entry = self.ltf.add_candle(c)

            if entry:
                await self._maybe_send_signal(
                    entry,
                    c["close"]
                )

        elif (
            symbol == self.secondary_symbol
            and granularity == LTF_GRANULARITY
        ):
            self.ltf_secondary.add_candle(c)

    async def _maybe_send_signal(self, entry, price):
        direction = entry["direction"]
        ob = entry["ob"]

        if (
            self.htf.trend is None
            or self.htf.trend != direction
        ):
            log.info(
                "[%s] Ishara %s imekataliwa: "
                "haiafiki HTF(15m) bias (%s).",
                self.display_name,
                direction.upper(),
                self.htf.trend,
            )
            return

        rsi_val = rsi(
            self.ltf_closes,
            RSI_PERIOD
        )

        sma_val = sma(
            self.ltf_closes,
            SMA_TREND
        )

        if rsi_val is None or sma_val is None:
            log.info(
                "[%s] Data haitoshi bado kwa RSI/SMA confluence.",
                self.display_name
            )
            return

        if (
            direction == "up"
            and (
                rsi_val >= RSI_OVERBOUGHT
                or price < sma_val
            )
        ):
            log.info(
                "[%s] Ishara UP imekataliwa na RSI/SMA filter.",
                self.display_name
            )
            return

        if (
            direction == "down"
            and (
                rsi_val <= RSI_OVERSOLD
                or price > sma_val
            )
        ):
            log.info(
                "[%s] Ishara DOWN imekataliwa na RSI/SMA filter.",
                self.display_name
            )
            return

        primary_swept = self.ltf.last_sweep
        secondary_swept = self.ltf_secondary.last_sweep

        if primary_swept is None:
            smt_note = (
                "ℹ️ Hakuna liquidity sweep dhahiri kabla ya "
                "CHoCH hii - SMT haikupimwa kwa ishara hii."
            )

        elif primary_swept == secondary_swept:
            smt_note = (
                "⚠️ Hakuna SMT divergence "
                "(pacha wa (1s) alifanya sweep ile ile - "
                "uthibitisho dhaifu)."
            )

        else:
            smt_note = (
                "✅ SMT divergence imethibitika "
                "(pacha wa (1s) HAKUFANYA sweep - "
                "uthibitisho mzuri)."
            )

        buffer = price * (SL_BUFFER_PCT / 100)

        if direction == "up":
            sl_price = ob["low"] - buffer
        else:
            sl_price = ob["high"] + buffer

        sl_distance = abs(
            price - sl_price
        )

        if sl_distance <= 0:
            log.warning(
                "[%s] sl_distance si sahihi (0), "
                "ishara imerukwa.",
                self.display_name
            )
            return

        tp_price = (
            price + RR_RATIO * sl_distance
            if direction == "up"
            else price - RR_RATIO * sl_distance
        )

        if (
            self.point_value
            and self.point_value > 0
        ):
            risk_amount = (
                ACCOUNT_BALANCE
                * (RISK_PERCENT_PER_TRADE / 100)
            )

            lot = risk_amount / (
                sl_distance * self.point_value
            )

            lot = max(
                round(lot, 2),
                0.01
            )

            lot_line = (
                f"📊 Lot Size (pendekezo): "
                f"<b>{lot}</b>\n"
            )

        else:
            lot_line = (
                "📊 Lot Size: weka POINT_VALUES "
                "ya symbol hii kwenye .env "
                "(angalia MT5 -> Specification) "
                "kupata pendekezo\n"
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
            f"bias={self.htf.trend.upper()} "
            f"+ LTF(1m) CHoCH+OB retest\n"
            f"RSI(14): {rsi_val:.1f} | "
            f"Bei dhidi ya SMA{SMA_TREND}: "
            f"{'juu' if price > sma_val else 'chini'}\n"
            f"{smt_note}\n"
            f"\n"
            f"⚠️ Hii ni PENDEKEZO TU "
            f"(si ushauri wa kifedha) - "
            f"fanya uamuzi wako mwenyewe "
            f"kabla ya kubonyeza kwenye MT5."
        )

        log.info(
            "[%s] ISHARA %s imetumwa. "
            "Entry: %.4f | TP: %.4f | SL: %.4f",
            self.display_name,
            direction.upper(),
            price,
            tp_price,
            sl_price,
        )


async def run_pair(monitor):
    client = PublicMarketClient()
    client.on_candle = monitor.on_candle

    backoff = 5
    max_backoff = 300

    while True:
        started_at = time.time()

        try:
            await client.connect()

            log.info(
                "[%s] Umeunganishwa (public data).",
                monitor.display_name
            )

            htf_hist = await client.get_candle_history(
                monitor.primary_symbol,
                HTF_GRANULARITY,
                CANDLE_COUNT,
            )

            for c in htf_hist:
                monitor.htf.add_candle(
                    _to_ohlc(c)
                )

            await client.subscribe_candles(
                monitor.primary_symbol,
                HTF_GRANULARITY,
            )

            ltf_hist = await client.get_candle_history(
                monitor.primary_symbol,
                LTF_GRANULARITY,
                CANDLE_COUNT,
            )

            for c in ltf_hist:
                cc = _to_ohlc(c)

                monitor.ltf_closes.append(
                    cc["close"]
                )

                monitor.ltf.add_candle(cc)

            await client.subscribe_candles(
                monitor.primary_symbol,
                LTF_GRANULARITY,
            )

            sec_hist = await client.get_candle_history(
                monitor.secondary_symbol,
                LTF_GRANULARITY,
                CANDLE_COUNT,
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
                time.time() - started_at
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

            await asyncio.sleep(backoff)

            backoff = min(
                backoff * 2,
                max_backoff
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
        p[2]
        for p in SYMBOL_PAIRS
    )

    await telegram.send(
        f"🤖 <b>Signal Bot v2 imeanza "
        f"(SMC/SMT + HTF/LTF + RSI/SMA)</b>\n"
        f"Symbols: {names}\n"
        f"HTF bias: {HTF_GRANULARITY}s | "
        f"LTF entry: {LTF_GRANULARITY}s\n\n"
        f"⚠️ Hii HAITRADE - "
        f"inatuma mapendekezo "
        f"(Entry/TP/SL/Lot) TU."
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
            *(run_pair(m) for m in monitors)
        )

    except KeyboardInterrupt:
        await telegram.send(
            "🛑 Signal bot imesimamishwa na mtumiaji."
        )


if __name__ == "__main__":
    asyncio.run(main())
