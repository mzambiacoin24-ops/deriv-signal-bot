import asyncio
import json
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

# Learning settings.
# The engine starts collecting immediately. It only influences decisions
# after enough completed examples exist, so a few early trades cannot
# distort the bot.
LEARNING_FILE = os.getenv(
    "LEARNING_FILE",
    "learning_data.json",
).strip()
LEARNING_MIN_SAMPLES = int(
    os.getenv("LEARNING_MIN_SAMPLES", "8")
)
LEARNING_MIN_WIN_RATE = float(
    os.getenv("LEARNING_MIN_WIN_RATE", "0.52")
)
LEARNING_LOOKBACK = int(
    os.getenv("LEARNING_LOOKBACK", "100")
)

BROKER_MIN_POINTS = {
    ("R_10", "2s"): 720,
    ("R_25", "2s"): 423,
    ("R_50", "2s"): 1350,
    ("R_75", "2s"): 10770,
    ("R_100", "2s"): 138,
    ("1HZ10V", "1s"): 106,
    ("1HZ25V", "1s"): 10215,
    ("1HZ50V", "1s"): 6996,
    ("1HZ75V", "1s"): 432,
    ("1HZ100V", "1s"): 72,
}

BROKER_POINT_SIZE = 0.01

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


class AdaptiveLearningEngine:
    """
    Persistent feedback/learning engine.

    It learns from completed TP/SL results.

    It does NOT rewrite the SMC rules. Instead it learns which combinations
    of conditions have historically worked for each index/feed.

    Feature key:
        symbol + feed + direction + setup + sweep + M15 + M5 + M1 +
        confidence + RR bucket

    Early trades are always collected. Once a feature has enough examples,
    a weak historical result can make the bot WAIT instead of sending the
    same type of signal blindly.
    """

    def __init__(
        self,
        path=LEARNING_FILE,
        min_samples=LEARNING_MIN_SAMPLES,
        min_win_rate=LEARNING_MIN_WIN_RATE,
        lookback=LEARNING_LOOKBACK,
    ):
        self.path = path
        self.min_samples = max(1, int(min_samples))
        self.min_win_rate = float(min_win_rate)
        self.lookback = max(20, int(lookback))
        self._lock = asyncio.Lock()
        self.data = {
            "version": 1,
            "updated_at": None,
            "trades": [],
        }
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return

            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)

            if isinstance(loaded, dict):
                trades = loaded.get("trades", [])
                if isinstance(trades, list):
                    self.data["trades"] = trades
                self.data["version"] = loaded.get("version", 1)
                self.data["updated_at"] = loaded.get("updated_at")

        except Exception as exc:
            log.warning(
                "Learning data haikuweza kusomwa: %s",
                exc,
            )

    def _save_unlocked(self):
        self.data["updated_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        temp_path = f"{self.path}.tmp"

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                self.data,
                handle,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(temp_path, self.path)

    @staticmethod
    def _rr_bucket(rr):
        try:
            rr = float(rr)
        except (TypeError, ValueError):
            return "unknown"

        if rr < 1.5:
            return "<1.5"
        if rr < 2.0:
            return "1.5-2"
        if rr < 3.0:
            return "2-3"
        return "3+"

    @staticmethod
    def _norm(value):
        if value is None:
            return "none"
        return str(value).strip().lower()

    def make_feature_key(self, features):
        parts = [
            self._norm(features.get("symbol")),
            self._norm(features.get("feed")),
            self._norm(features.get("direction")),
            self._norm(features.get("setup")),
            self._norm(features.get("sweep")),
            self._norm(features.get("m15")),
            self._norm(features.get("m5")),
            self._norm(features.get("m1")),
            self._norm(features.get("confidence")),
            self._rr_bucket(features.get("rr")),
        ]
        return "|".join(parts)

    def _matching_trades_unlocked(self, feature_key):
        matches = [
            item
            for item in self.data["trades"]
            if item.get("feature_key") == feature_key
            and item.get("result") in ("tp", "sl")
        ]

        return matches[-self.lookback:]

    def stats(self, features):
        key = self.make_feature_key(features)

        matches = self._matching_trades_unlocked(key)

        total = len(matches)
        wins = sum(
            1
            for item in matches
            if item.get("result") == "tp"
        )
        losses = total - wins

        win_rate = (
            wins / total
            if total
            else None
        )

        return {
            "feature_key": key,
            "samples": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
        }

    async def register_signal(self, features):
        async with self._lock:
            key = self.make_feature_key(features)

            record = {
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "feature_key": key,
                "symbol": features.get("symbol"),
                "feed": features.get("feed"),
                "direction": features.get("direction"),
                "setup": features.get("setup"),
                "sweep": features.get("sweep"),
                "m15": features.get("m15"),
                "m5": features.get("m5"),
                "m1": features.get("m1"),
                "confidence": features.get("confidence"),
                "rr": features.get("rr"),
                "entry": features.get("entry"),
                "tp": features.get("tp"),
                "sl": features.get("sl"),
                "entry_epoch": features.get("entry_epoch"),
                "result": None,
                "exit": None,
                "exit_epoch": None,
            }

            self.data["trades"].append(record)

            if len(self.data["trades"]) > 5000:
                self.data["trades"] = self.data["trades"][-5000:]

            self._save_unlocked()

            return len(self.data["trades"]) - 1

    async def register_result(
        self,
        feature_key,
        result,
        exit_price=None,
        exit_epoch=None,
    ):
        result = str(result).lower()

        if result not in ("tp", "sl"):
            return

        async with self._lock:
            # Find the newest unresolved matching trade.
            for item in reversed(self.data["trades"]):
                if (
                    item.get("feature_key") == feature_key
                    and item.get("result") is None
                ):
                    item["result"] = result
                    item["exit"] = exit_price
                    item["exit_epoch"] = exit_epoch
                    item["completed_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    break

            self._save_unlocked()

    async def evaluate(self, features):
        async with self._lock:
            stats = self.stats(features)

            # Not enough historical evidence:
            # do not punish a new setup.
            if stats["samples"] < self.min_samples:
                return {
                    "decision": "LEARN",
                    **stats,
                }

            if (
                stats["win_rate"] is not None
                and stats["win_rate"] >= self.min_win_rate
            ):
                return {
                    "decision": "ALLOW",
                    **stats,
                }

            return {
                "decision": "WAIT",
                **stats,
            }

    async def report(self):
        async with self._lock:
            completed = [
                x
                for x in self.data["trades"]
                if x.get("result") in ("tp", "sl")
            ]

            wins = sum(
                1
                for x in completed
                if x.get("result") == "tp"
            )

            losses = len(completed) - wins

            rate = (
                wins / len(completed)
                if completed
                else 0.0
            )

            return {
                "total_completed": len(completed),
                "wins": wins,
                "losses": losses,
                "win_rate": rate,
                "stored": len(self.data["trades"]),
            }


def entry_timing_ok(direction, structure):
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

    if latest_range > avg_range * 1.60:
        return False

    if direction == "down":
        pullback = (
            float(c2["high"]) > float(c1["high"])
            or float(c2["close"]) > float(c1["close"])
        )

        confirmation = (
            float(c3["close"]) < float(c2["close"])
            and float(c3["close"]) < float(c3["open"])
        )

        if not (pullback and confirmation):
            return False

        pullback_size = (
            float(c2["high"])
            - min(
                float(c["low"])
                for c in candles[-7:-3]
            )
        )

        if pullback_size < avg_range * 0.35:
            return False

        recent_low = min(
            float(c["low"])
            for c in candles[-7:-1]
        )

        room_below = (
            float(c3["close"]) - recent_low
        )

        if room_below < avg_range * 0.50:
            return False

        if (
            float(c3["close"])
            < float(c2["low"]) - avg_range * 0.25
        ):
            return False

        return True

    pullback = (
        float(c2["low"]) < float(c1["low"])
        or float(c2["close"]) < float(c1["close"])
    )

    confirmation = (
        float(c3["close"]) > float(c2["close"])
        and float(c3["close"]) > float(c3["open"])
    )

    if not (pullback and confirmation):
        return False

    pullback_size = (
        max(
            float(c["high"])
            for c in candles[-7:-3]
        )
        - float(c2["low"])
    )

    if pullback_size < avg_range * 0.35:
        return False

    recent_high = max(
        float(c["high"])
        for c in candles[-7:-1]
    )

    room_above = (
        recent_high - float(c3["close"])
    )

    if room_above < avg_range * 0.50:
        return False

    if (
        float(c3["close"])
        > float(c2["high"]) + avg_range * 0.25
    ):
        return False

    return True


def calculate_levels(
    direction,
    entry,
    structure,
    symbol=None,
    feed_label=None,
):
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

    min_points = 0
    if symbol is not None and feed_label is not None:
        min_points = BROKER_MIN_POINTS.get(
            (symbol, feed_label),
            0,
        )

    min_distance = (
        min_points * BROKER_POINT_SIZE
        if min_points > 0
        else 0.0
    )

    practical_min_distance = (
        min_distance * 1.05
        if min_distance > 0
        else 0.0
    )

    if direction == "up":
        usable_lows = [x for x in lows if x < entry]

        if usable_lows:
            support = max(usable_lows[-5:])
            structure_sl = min(
                support - avg_range * 0.20,
                entry - avg_range * 0.80,
            )
        else:
            structure_sl = entry - avg_range

        sl = min(
            structure_sl,
            entry - practical_min_distance,
        )

        risk = entry - sl

        if risk <= 0:
            return None, None

        minimum_tp_distance = max(
            practical_min_distance,
            risk * MIN_RR_RATIO,
        )

        usable_highs = [x for x in highs if x > entry]

        structural_tp = None
        if usable_highs:
            candidates = [
                x for x in usable_highs[-5:]
                if (x - entry) >= minimum_tp_distance
            ]

            if candidates:
                structural_tp = min(candidates)

        base_tp = entry + minimum_tp_distance

        if structural_tp is not None:
            max_practical_distance = max(
                minimum_tp_distance,
                minimum_tp_distance + avg_range,
            )

            if (
                structural_tp - entry
                <= max_practical_distance
            ):
                tp = structural_tp
            else:
                tp = base_tp
        else:
            tp = base_tp

        return sl, tp

    usable_highs = [x for x in highs if x > entry]

    if usable_highs:
        resistance = min(usable_highs[-5:])
        structure_sl = max(
            resistance + avg_range * 0.20,
            entry + avg_range * 0.80,
        )
    else:
        structure_sl = entry + avg_range

    sl = max(
        structure_sl,
        entry + practical_min_distance,
    )

    risk = sl - entry

    if risk <= 0:
        return None, None

    minimum_tp_distance = max(
        practical_min_distance,
        risk * MIN_RR_RATIO,
    )

    usable_lows = [x for x in lows if x < entry]

    structural_tp = None
    if usable_lows:
        candidates = [
            x for x in usable_lows[-5:]
            if (entry - x) >= minimum_tp_distance
        ]

        if candidates:
            structural_tp = max(candidates)

    base_tp = entry - minimum_tp_distance

    if structural_tp is not None:
        max_practical_distance = max(
            minimum_tp_distance,
            minimum_tp_distance + avg_range,
        )

        if (
            entry - structural_tp
            <= max_practical_distance
        ):
            tp = structural_tp
        else:
            tp = base_tp
    else:
        tp = base_tp

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
        learning,
    ):
        self.symbol = symbol
        self.display_name = display_name
        self.feed_label = feed_label
        self.point_symbol = point_symbol
        self.telegram = telegram
        self.tracker = tracker
        self.memory = memory
        self.learning = learning

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
        self.active_learning_key = None

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
        learning_key = self.active_learning_key

        self.active_event_id = None
        self.active_learning_key = None

        if event_id:
            self.memory.record_result(
                self.symbol,
                event_id,
                completed["result"].lower(),
                exit_price=completed["exit"],
                exit_epoch=epoch,
            )

        if learning_key:
            await self.learning.register_result(
                learning_key,
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
            f"🧠 Result imeongezwa kwenye learning engine.\n"
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
            symbol=self.symbol,
            feed_label=self.feed_label,
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

        # ---------------------------------------------------------
        # LEARNING / FEEDBACK
        # ---------------------------------------------------------
        learning_features = {
            "symbol": self.symbol,
            "feed": self.feed_label,
            "direction": htf_direction,
            "setup": actual_setup,
            "sweep": sweep,
            "m15": self.htf.trend,
            "m5": self.mtf.trend,
            "m1": ltf_direction,
            "confidence": confidence,
            "rr": rr,
            "entry": price,
            "tp": tp,
            "sl": sl,
            "entry_epoch": int(
                candle.get(
                    "epoch",
                    time.time(),
                )
            ),
        }

        learning_decision = await self.learning.evaluate(
            learning_features
        )

        if learning_decision["decision"] == "WAIT":
            log.info(
                "[%s | %s] LEARNING WAIT | "
                "samples=%d wins=%d losses=%d win_rate=%.1f%%",
                self.display_name,
                self.feed_label,
                learning_decision["samples"],
                learning_decision["wins"],
                learning_decision["losses"],
                (
                    learning_decision["win_rate"] * 100
                    if learning_decision["win_rate"] is not None
                    else 0
                ),
            )
            return

        learning_label = "LEARNING"
        if learning_decision["decision"] == "ALLOW":
            learning_label = (
                f"LEARNED PASS "
                f"({learning_decision['win_rate'] * 100:.1f}%/"
                f"{learning_decision['samples']})"
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

        if learning_decision["decision"] == "LEARN":
            learning_text = (
                "🧠 Learning: <b>COLLECTING DATA</b>\n"
                f"Samples za setup hii: "
                f"<b>{learning_decision['samples']}</b>"
            )
        else:
            learning_text = (
                f"🧠 Learning: <b>{learning_label}</b>\n"
                f"Historical samples: "
                f"<b>{learning_decision['samples']}</b>\n"
                f"Historical TP rate: "
                f"<b>{learning_decision['win_rate'] * 100:.1f}%</b>"
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
            f"🧩 Setup: <b>{actual_setup}</b>\n"
            f"⏱️ Entry Timing: <b>CONFIRMED</b>\n"
            f"{learning_text}\n\n"
            f"⚠️ <i>Hii ni pendekezo la analysis tu, "
            f"si ushauri wa kifedha.</i>"
        )

        try:
            await self.telegram.send(message)

            signal_data = {
                **learning_features,
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

            # Store the same feature key that will be used when
            # TP/SL arrives.
            self.active_learning_key = (
                self.learning.make_feature_key(
                    learning_features
                )
            )

            await self.learning.register_signal(
                learning_features
            )

            self.active_event_id = event_id
            self.last_signal_time = now
            self.last_signal_direction = htf_direction

            log.info(
                "[%s | %s] SIGNAL %s | "
                "entry=%.4f sl=%.4f tp=%.4f "
                "RR=%.2f | learning=%s | event=%s",
                self.display_name,
                self.feed_label,
                action,
                price,
                sl,
                tp,
                rr,
                learning_label,
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

            result = str(
                event.get("result", "")
            ).lower()

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


async def _build_performance_report(
    memory,
    learning,
):
    now = time.time()

    windows = [
        ("24 HOURS", 24 * 60 * 60),
        ("7 DAYS", 7 * 24 * 60 * 60),
        ("30 DAYS", 30 * 24 * 60 * 60),
    ]

    learning_stats = await learning.report()

    lines = [
        "📊 <b>SIGNAL BOT PERFORMANCE REPORT</b>",
        "",
        (
            "🧠 Learning engine: "
            f"<b>{learning_stats['stored']}</b> records"
        ),
        (
            "📚 Completed learning samples: "
            f"<b>{learning_stats['total_completed']}</b>"
        ),
        (
            "🧠 Overall learned TP rate: "
            f"<b>{learning_stats['win_rate'] * 100:.1f}%</b>"
        ),
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
        "🧠 Learning engine inatumia matokeo ya TP/SL "
        "kuboresha maamuzi ya setup zinazojirudia."
    )

    return "\n".join(lines)


async def performance_report_loop(
    telegram,
    memory,
    learning,
):
    while True:
        try:
            await asyncio.sleep(24 * 60 * 60)

            message = await _build_performance_report(
                memory,
                learning,
            )

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

    learning = AdaptiveLearningEngine()

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
            learning,
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
            "🤖 <b>Signal Bot v8</b>\n\n"
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
            "🧠 <b>ADAPTIVE LEARNING: ACTIVE</b>\n"
            f"Learning inaanza kutumia historia baada ya "
            f"<b>{LEARNING_MIN_SAMPLES}</b> samples za setup inayofanana.\n"
            f"Minimum learned TP rate: "
            f"<b>{LEARNING_MIN_WIN_RATE * 100:.0f}%</b>\n\n"
            "⚠️ Analysis only."
        )

    except Exception as exc:
        log.error(
            "Telegram startup message failed: %s",
            exc,
        )

    report_task = asyncio.create_task(
        performance_report_loop(
            telegram,
            memory,
            learning,
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
