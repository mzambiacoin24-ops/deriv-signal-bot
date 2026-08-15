import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from dotenv import load_dotenv

from public_client import PublicMarketClient
from smc import SMCAnalyzer
from indicators import sma, rsi
from telegram_notifier import TelegramNotifier
from trade_tracker import TradeTracker
from memory import SymbolMemory


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("signal-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CANDLE_COUNT = int(os.getenv("SIGNAL_CANDLE_COUNT", "200"))
RSI_PERIOD = int(os.getenv("SIGNAL_RSI_PERIOD", "14"))
SMA_TREND = int(os.getenv("SIGNAL_SMA_TREND", "50"))
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "1.30"))
MIN_SECONDS_BETWEEN_SIGNALS = int(
    os.getenv("MIN_SECONDS_BETWEEN_SIGNALS", "900")
)
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PERCENT_PER_TRADE = float(
    os.getenv("RISK_PERCENT_PER_TRADE", "1")
)

POINT_VALUES = {}
for item in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1",
).split(","):
    if "=" not in item:
        continue
    symbol, value = item.split("=", 1)
    try:
        POINT_VALUES[symbol.strip()] = float(value.strip())
    except ValueError:
        pass

SYMBOLS = [
    ("R_10", "Volatility 10 Index", "2s", "R_10"),
    ("1HZ10V", "Volatility 10 (1s) Index", "1s", "R_10"),
    ("R_25", "Volatility 25 Index", "2s", "R_25"),
    ("1HZ25V", "Volatility 25 (1s) Index", "1s", "R_25"),
    ("R_50", "Volatility 50 Index", "2s", "R_50"),
    ("1HZ50V", "Volatility 50 (1s) Index", "1s", "R_50"),
    ("R_75", "Volatility 75 Index", "2s", "R_75"),
    ("1HZ75V", "Volatility 75 (1s) Index", "1s", "R_75"),
    ("R_100", "Volatility 100 Index", "2s", "R_100"),
    ("1HZ100V", "Volatility 100 (1s) Index", "1s", "R_100"),
]


def clean_candle(candle):
    return {
        "epoch": int(candle["epoch"]),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "granularity": int(candle.get("granularity", 60)),
    }


def entry_timing_ok(direction, structure):
    """
    ENTRY TIMING ONLY.

    The existing M15/M5/M1 direction, A-Class filter, sweep filter,
    tracker, memory, TP/SL and all other logic remain unchanged.

    The purpose here is only to avoid late/chasing entries and wait
    for a meaningful pullback followed by M1 continuation confirmation.
    """
    candles = list(structure.candles)

    if len(candles) < 10:
        return False

    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]

    def candle_range(c):
        return abs(
            float(c["high"]) - float(c["low"])
        )

    recent = candles[-10:-1]

    ranges = [
        candle_range(c)
        for c in recent
        if candle_range(c) > 0
    ]

    if not ranges:
        return False

    avg_range = sum(ranges) / len(ranges)

    latest_range = candle_range(c3)

    # Do not enter on an unusually large M1 candle.
    if latest_range > avg_range * 1.60:
        return False

    # -------------------------------------------------------------
    # SELL ENTRY
    # -------------------------------------------------------------
    if direction == "down":
        # c2 must represent a real upward pullback/recovery.
        pullback = (
            float(c2["high"]) > float(c1["high"])
            or float(c2["close"]) > float(c1["close"])
        )

        # c3 must then confirm renewed bearish pressure.
        confirmation = (
            float(c3["close"]) < float(c2["close"])
            and float(c3["close"]) < float(c3["open"])
        )

        if not (pullback and confirmation):
            return False

        # The pullback must have meaningful size.
        pullback_size = (
            float(c2["high"])
            - min(
                float(c["low"])
                for c in candles[-7:-3]
            )
        )

        if pullback_size < avg_range * 0.35:
            return False

        # Do not SELL directly on/near the recent low.
        # There must still be room below the entry.
        recent_low = min(
            float(c["low"])
            for c in candles[-7:-1]
        )

        room_below = (
            float(c3["close"]) - recent_low
        )

        if room_below < avg_range * 0.50:
            return False

        # Confirmation should not be another full-size breakdown.
        # This prevents chasing a move that has already accelerated.
        if (
            float(c3["close"]) <
            float(c2["low"]) - avg_range * 0.25
        ):
            return False

        return True

    # -------------------------------------------------------------
    # BUY ENTRY
    # -------------------------------------------------------------
    # c2 must represent a real downward pullback/recovery.
    pullback = (
        float(c2["low"]) < float(c1["low"])
        or float(c2["close"]) < float(c1["close"])
    )

    # c3 must then confirm renewed bullish pressure.
    confirmation = (
        float(c3["close"]) > float(c2["close"])
        and float(c3["close"]) > float(c3["open"])
    )

    if not (pullback and confirmation):
        return False

    # The pullback must have meaningful size.
    pullback_size = (
        max(
            float(c["high"])
            for c in candles[-7:-3]
        )
        - float(c2["low"])
    )

    if pullback_size < avg_range * 0.35:
        return False

    # Do not BUY directly on/near the recent high.
    # There must still be room above the entry.
    recent_high = max(
        float(c["high"])
        for c in candles[-7:-1]
    )

    room_above = (
        recent_high - float(c3["close"])
    )

    if room_above < avg_range * 0.50:
        return False

    # Confirmation should not be another full-size breakout.
    # This prevents chasing an already accelerated move.
    if (
        float(c3["close"]) >
        float(c2["high"]) + avg_range * 0.25
    ):
        return False

    return True


def calculate_levels(direction, entry, structure):
    highs = list(structure.swing_highs)
    lows = list(structure.swing_lows)
    candles = list(structure.candles)

    if len(candles) < 10:
        return None, None

    ranges = [
        abs(c["high"] - c["low"])
        for c in candles[-20:]
        if abs(c["high"] - c["low"]) > 0
    ]

    if not ranges:
        return None, None

    avg_range = sum(ranges) / len(ranges)

    if direction == "up":
        usable_lows = [x for x in lows if x < entry]

        if usable_lows:
            support = max(usable_lows[-5:])
            sl = min(
                support - avg_range * 0.20,
                entry - avg_range * 0.80,
            )
        else:
            sl = entry - avg_range

        risk = entry - sl

        if risk <= 0:
            return None, None

        usable_highs = [x for x in highs if x > entry]

        if usable_highs:
            resistance = min(usable_highs[-5:])
            minimum_tp = entry + risk * MIN_RR_RATIO
            tp = max(resistance, minimum_tp)
        else:
            tp = entry + risk * MIN_RR_RATIO

        return sl, tp

    usable_highs = [x for x in highs if x > entry]

    if usable_highs:
        resistance = min(usable_highs[-5:])
        sl = max(
            resistance + avg_range * 0.20,
            entry + avg_range * 0.80,
        )
    else:
        sl = entry + avg_range

    risk = sl - entry

    if risk <= 0:
        return None, None

    usable_lows = [x for x in lows if x < entry]

    if usable_lows:
        support = max(usable_lows[-5:])
        minimum_tp = entry - risk * MIN_RR_RATIO
        tp = min(support, minimum_tp)
    else:
        tp = entry - risk * MIN_RR_RATIO

    return sl, tp


class TimeframeBuilder:
    def __init__(self, seconds):
        self.seconds = seconds
        self.current = None

    def update(self, candle):
        epoch = int(candle["epoch"])
        bucket = epoch - (epoch % self.seconds)

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

        if bucket == self.current["epoch"]:
            self.current["high"] = max(
                self.current["high"],
                candle["high"],
            )
            self.current["low"] = min(
                self.current["low"],
                candle["low"],
            )
            self.current["close"] = candle["close"]
            return None

        completed = dict(self.current)

        self.current = {
            "epoch": bucket,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "granularity": self.seconds,
        }

        return completed


class PairMonitor:
    def __init__(
        self,
        symbol,
        display_name,
        feed_label,
        point_symbol,
        telegram,
        tracker,
        memory,
    ):
        self.symbol = symbol
        self.display_name = display_name
        self.feed_label = feed_label
        self.point_symbol = point_symbol
        self.telegram = telegram
        self.tracker = tracker
        self.memory = memory

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

        self.mtf_builder = TimeframeBuilder(300)
        self.htf_builder = TimeframeBuilder(900)
        self.ltf_builder = TimeframeBuilder(60)

        self.ltf_closes = deque(maxlen=100)

        self.last_signal_time = 0
        self.last_signal_direction = None
        self.active_event_id = None

        self.point_value = POINT_VALUES.get(point_symbol)
        self.ready = False

    async def initialize(self, client):
        log.info(
            "[%s | %s] Inapakua history...",
            self.display_name,
            self.feed_label,
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

            for candle in htf_data:
                self.htf.add_candle(clean_candle(candle))

            for candle in mtf_data:
                self.mtf.add_candle(clean_candle(candle))

            for candle in ltf_data:
                c = clean_candle(candle)
                self.ltf_closes.append(c["close"])
                self.ltf.add_candle(c)

            if mtf_data:
                self.mtf_builder.current = clean_candle(mtf_data[-1])

            if htf_data:
                self.htf_builder.current = clean_candle(htf_data[-1])

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

    async def _track_active_trade(self, price, epoch):
        if not self.tracker.is_active(
            self.symbol,
            self.feed_label,
        ):
            return

        completed = self.tracker.check_price(
            self.symbol,
            self.feed_label,
            price,
        )

        if completed is None:
            return

        event_id = self.active_event_id
        self.active_event_id = None

        if event_id:
            self.memory.record_result(
                self.symbol,
                event_id,
                completed["result"].lower(),
                exit_price=completed["exit"],
                exit_epoch=epoch,
            )

        result = completed["result"]
        icon = "✅" if result == "TP" else "🛑"

        feed_name = (
            "1 SECOND (1s)"
            if self.feed_label == "1s"
            else "2 SECONDS (2s)"
        )

        message = (
            f"{icon} <b>TRADE IMEKAMILIKA</b>\n\n"
            f"📡 FEED: <b>{feed_name}</b>\n"
            f"📌 Deriv Symbol: <b>{self.symbol}</b>\n"
            f"📍 Symbol (MT5): <b>{self.display_name}</b>\n"
            f"📊 Result: <b>{result}</b>\n"
            f"💰 Entry: <b>{completed['entry']:.4f}</b>\n"
            f"🏁 Exit: <b>{completed['exit']:.4f}</b>\n"
            f"🎯 TP: <b>{completed['tp']:.4f}</b>\n"
            f"🛑 SL: <b>{completed['sl']:.4f}</b>\n"
            f"⏱️ Duration: <b>{completed['duration_seconds']:.0f}s</b>\n\n"
            f"🔓 Symbol iko tayari kuchambuliwa kwa signal mpya."
        )

        try:
            await self.telegram.send(message)
        except Exception as exc:
            log.exception(
                "[%s | %s] Result Telegram error: %s",
                self.display_name,
                self.feed_label,
                exc,
            )

    async def on_candle(self, symbol, candle):
        if symbol != self.symbol:
            return

        if not self.ready:
            return

        granularity = int(candle.get("granularity", 60))

        if granularity != 60:
            return

        c = clean_candle(candle)

        await self._track_active_trade(
            c["close"],
            int(candle.get("tick_epoch", c["epoch"])),
        )

        completed_m1 = self.ltf_builder.update(c)

        if completed_m1 is None:
            return

        self.ltf_closes.append(completed_m1["close"])

        ltf_entry = self.ltf.add_candle(completed_m1)

        completed_m5 = self.mtf_builder.update(completed_m1)

        if completed_m5:
            self.mtf.add_candle(completed_m5)

        completed_m15 = self.htf_builder.update(completed_m1)

        if completed_m15:
            self.htf.add_candle(completed_m15)

        if ltf_entry:
            await self.evaluate_signal(
                ltf_entry,
                completed_m1,
            )

    async def evaluate_signal(self, ltf_setup, candle):
        now = time.time()

        if self.tracker.is_active(
            self.symbol,
            self.feed_label,
        ):
            return

        if (
            now - self.last_signal_time
            < MIN_SECONDS_BETWEEN_SIGNALS
        ):
            return

        htf_direction = self.htf.trend

        if htf_direction not in ("up", "down"):
            return

        mtf_direction = self.mtf.trend

        if mtf_direction != htf_direction:
            return

        ltf_direction = ltf_setup.get("direction")

        if ltf_direction != htf_direction:
            return

        if self.htf.structure_strength == "NEUTRAL":
            return

        if self.mtf.structure_strength == "NEUTRAL":
            return

        price = float(candle["close"])

        rsi_value = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        sma_value = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        rsi_ok = True

        if rsi_value is not None:
            if htf_direction == "up":
                rsi_ok = rsi_value < 75
            else:
                rsi_ok = rsi_value > 25

        sma_ok = True

        if sma_value is not None:
            if htf_direction == "up":
                sma_ok = price >= sma_value
            else:
                sma_ok = price <= sma_value

        confluence = 0

        if rsi_ok:
            confluence += 1

        if sma_ok:
            confluence += 1

        if ltf_setup.get("sweep") is not None:
            confluence += 1

        # =========================================================
        # ENTRY TIMING ONLY
        # Existing direction/SMC logic remains unchanged.
        # Wait for pullback + continuation confirmation and
        # avoid chasing an already extended move.
        # =========================================================
        if not entry_timing_ok(
            htf_direction,
            self.ltf,
        ):
            log.info(
                "[%s | %s] ENTRY TIMING WAIT | direction=%s",
                self.display_name,
                self.feed_label,
                htf_direction,
            )
            return

        sl, tp = calculate_levels(
            htf_direction,
            price,
            self.htf,
        )

        if sl is None or tp is None:
            return

        risk = abs(price - sl)
        reward = abs(tp - price)

        if risk <= 0:
            return

        rr = reward / risk

        if rr < MIN_RR_RATIO:
            return

        if (
            self.last_signal_direction == htf_direction
            and (
                now - self.last_signal_time
                < MIN_SECONDS_BETWEEN_SIGNALS * 2
            )
        ):
            return

        # A-CLASS TELEGRAM FILTER
        # Sweep lazima iwe high au low.
        # Sweep None haitumwi Telegram.
        sweep = ltf_setup.get("sweep")

        expected_setup = (
            "BULLISH_PULLBACK"
            if htf_direction == "up"
            else "BEARISH_PULLBACK"
        )

        actual_setup = ltf_setup.get(
            "reason",
            "SMC",
        )

        a_grade = (
            self.htf.structure_strength == "STRONG"
            and self.mtf.structure_strength == "STRONG"
            and ltf_direction == htf_direction
            and actual_setup == expected_setup
            and sweep in ("high", "low")
            and rr >= MIN_RR_RATIO
        )

        if not a_grade:
            log.info(
                "[%s | %s] NON-A setup ignored | "
                "direction=%s M15=%s M5=%s M1=%s "
                "sweep=%s setup=%s RR=%.2f",
                self.display_name,
                self.feed_label,
                htf_direction,
                self.htf.structure_strength,
                self.mtf.structure_strength,
                ltf_direction,
                sweep,
                actual_setup,
                rr,
            )
            return

        if (
            htf_direction == "up"
            and self.htf.structure_strength == "STRONG"
            and self.mtf.structure_strength == "STRONG"
        ):
            confidence = "HIGH"
        elif confluence >= 2:
            confidence = "GOOD"
        else:
            confidence = "STANDARD"

        lot = None

        if self.point_value is not None and self.point_value > 0:
            risk_money = ACCOUNT_BALANCE * (
                RISK_PERCENT_PER_TRADE / 100
            )

            lot = risk_money / (
                risk * self.point_value
            )

            lot = max(
                round(lot, 2),
                0.01,
            )

        if htf_direction == "up":
            action = "NUNUA (BUY)"
            icon = "📈"
        else:
            action = "UZA (SELL)"
            icon = "📉"

        if rsi_value is None:
            rsi_text = "N/A"
        else:
            rsi_text = f"{rsi_value:.1f}"

        if sma_value is None:
            sma_text = "N/A"
        elif price >= sma_value:
            sma_text = "JUU"
        else:
            sma_text = "CHINI"

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

        lot_text = (
            f"{lot}"
            if lot is not None
            else "N/A"
        )

        feed_name = (
            "1 SECOND (1s)"
            if self.feed_label == "1s"
            else "2 SECONDS (2s)"
        )

        message = (
            f"{icon} <b>ISHARA: {action}</b>\n\n"
            f"📡 <b>FEED: {feed_name}</b>\n"
            f"📌 Deriv Symbol: <b>{self.symbol}</b>\n"
            f"Symbol (MT5): <b>{self.display_name}</b>\n"
            f"🎯 Confidence: <b>{confidence}</b>\n"
            f"💰 Entry: <b>{price:.4f}</b>\n"
            f"🎯 Take Profit: <b>{tp:.4f}</b> ({reward:.4f})\n"
            f"🛑 Stop Loss: <b>{sl:.4f}</b> ({risk:.4f})\n"
            f"⚖️ R:R: <b>1:{rr:.2f}</b>\n"
            f"📊 Lot Size: <b>{lot_text}</b>\n\n"
            f"📐 Market Structure: <b>{htf_direction.upper()}</b>\n"
            f"🧠 M15: <b>{self.htf.trend.upper()}</b> "
            f"({self.htf.structure_strength})\n"
            f"🔄 M5: <b>{self.mtf.trend.upper()}</b> "
            f"({self.mtf.structure_strength})\n"
            f"⚡ M1: <b>{ltf_direction.upper()}</b>\n"
            f"📊 RSI({RSI_PERIOD}): {rsi_text}\n"
            f"📏 SMA{SMA_TREND}: {sma_text}\n"
            f"💧 Liquidity Sweep: {sweep_text}\n"
            f"🧩 Setup: <b>{ltf_setup.get('reason', 'SMC')}</b>\n\n"
            f"⚠️ <i>Hii ni pendekezo la analysis tu, "
            f"si ushauri wa kifedha.</i>"
        )

        try:
            await self.telegram.send(message)

            signal_data = {
                "direction": htf_direction,
                "entry": price,
                "tp": tp,
                "sl": sl,
                "rr": rr,
                "setup": ltf_setup.get("reason", "SMC"),
                "sweep": sweep,
                "confidence": confidence,
                "m15": self.htf.trend,
                "m5": self.mtf.trend,
                "m1": ltf_direction,
                "rsi": rsi_value,
                "sma": sma_text,
                "entry_epoch": int(
                    candle.get(
                        "epoch",
                        time.time(),
                    )
                ),
            }

            event_id = self.memory.record_signal(
                self.symbol,
                self.feed_label,
                self.display_name,
                signal_data,
            )

            registered = self.tracker.register(
                self.symbol,
                self.feed_label,
                htf_direction,
                price,
                tp,
                sl,
                self.display_name,
            )

            if not registered:
                log.warning(
                    "[%s | %s] Tracker already active "
                    "after signal send.",
                    self.display_name,
                    self.feed_label,
                )
                return

            self.active_event_id = event_id
            self.last_signal_time = now
            self.last_signal_direction = htf_direction

            log.info(
                "[%s | %s] SIGNAL %s | "
                "entry=%.4f sl=%.4f tp=%.4f "
                "RR=%.2f | event=%s",
                self.display_name,
                self.feed_label,
                action,
                price,
                sl,
                tp,
                rr,
                event_id,
            )

        except Exception as exc:
            log.exception(
                "[%s | %s] Telegram/signal error: %s",
                self.display_name,
                self.feed_label,
                exc,
            )


def _report_stats(memory, since_epoch):
    """Count completed TP/SL results and total signals in a time window."""
    signals = 0
    tp = 0
    sl = 0

    for item in memory.data.get("symbols", {}).values():
        if not isinstance(item, dict):
            continue

        for event in item.get("events", []):
            if not isinstance(event, dict):
                continue

            created_at = event.get("created_at")
            if not created_at:
                continue

            try:
                created_epoch = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                continue

            if created_epoch < since_epoch:
                continue

            signals += 1

            result = str(event.get("result", "")).lower()

            if result == "tp":
                tp += 1
            elif result == "sl":
                sl += 1

    closed = tp + sl

    win_rate = (
        (tp / closed) * 100
        if closed
        else 0.0
    )

    return signals, tp, sl, win_rate


def _build_performance_report(memory):
    """Build the 24h/7d/30d Telegram performance report."""
    now = time.time()

    windows = [
        ("24 HOURS", 24 * 60 * 60),
        ("7 DAYS", 7 * 24 * 60 * 60),
        ("30 DAYS", 30 * 24 * 60 * 60),
    ]

    lines = [
        "📊 <b>SIGNAL BOT PERFORMANCE REPORT</b>",
        "",
        f"🕐 Report time: <b>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</b>",
        "",
    ]

    for label, seconds in windows:
        signals, tp, sl, win_rate = _report_stats(
            memory,
            now - seconds,
        )

        lines.extend(
            [
                f"📅 <b>{label}</b>",
                f"📡 Signals: <b>{signals}</b>",
                f"✅ TP: <b>{tp}</b>",
                f"🛑 SL: <b>{sl}</b>",
                f"📈 TP Rate: <b>{win_rate:.1f}%</b>",
                "",
            ]
        )

    lines.append(
        "🧠 Report inatumia matokeo yaliyohifadhiwa "
        "kwenye Symbol Memory."
    )

    return "\n".join(lines)


async def performance_report_loop(telegram, memory):
    """Send one performance report every 24 hours."""
    while True:
        try:
            await asyncio.sleep(24 * 60 * 60)

            message = _build_performance_report(memory)

            try:
                await telegram.send(message)
            except Exception as exc:
                log.exception(
                    "Performance report Telegram error: %s",
                    exc,
                )

        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception(
                "Performance report loop error: %s",
                exc,
            )
            await asyncio.sleep(60)


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

    tracker = TradeTracker()
    memory = SymbolMemory()

    client = PublicMarketClient(
        timeout=20
    )

    await client.connect()

    monitors = []

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
            tracker,
            memory,
        )
        monitors.append(monitor)

    for monitor in monitors:
        await monitor.initialize(client)

    async def callback(symbol, candle):
        for monitor in monitors:
            if monitor.symbol == symbol:
                await monitor.on_candle(
                    symbol,
                    candle,
                )
                break

    client.on_candle = callback

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

            await asyncio.sleep(0.5)

        except Exception as exc:
            log.exception(
                "[%s | %s] Stream start error: %s",
                monitor.display_name,
                monitor.feed_label,
                exc,
            )

    try:
        await telegram.send(
            "🤖 <b>Signal Bot v7</b>\n\n"
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
            "📡 Kila signal inaonyesha feed yake.\n"
            "🔒 Symbol yenye signal active "
            "haitapewa signal nyingine "
            "mpaka TP au SL ifikiwe.\n"
            "🧠 Matokeo yanawekwa kwenye "
            "symbol memory.\n\n"
            "⚠️ Analysis only."
        )

    except Exception as exc:
        log.error(
            "Telegram startup message failed: %s",
            exc,
        )

    # =============================================================
    # PERFORMANCE REPORT
    # One report every 24 hours containing:
    # 24h + 7d + 30d signal/TP/SL statistics.
    # =============================================================
    report_task = asyncio.create_task(
        performance_report_loop(
            telegram,
            memory,
        )
    )

    try:
        while True:
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        pass

    finally:
        report_task.cancel()
        try:
            await report_task
        except asyncio.CancelledError:
            pass

        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
