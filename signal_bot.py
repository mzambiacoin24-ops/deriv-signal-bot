
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
RISK_PERCENT_PER_TRADE = float(os.getenv("RISK_PERCENT_PER_TRADE", "1"))

# Learning only starts affecting a setup after enough completed examples.
LEARNING_FILE = os.getenv("LEARNING_FILE", "learning_data.json").strip()
LEARNING_MIN_SAMPLES = int(os.getenv("LEARNING_MIN_SAMPLES", "8"))
LEARNING_MIN_WIN_RATE = float(os.getenv("LEARNING_MIN_WIN_RATE", "0.52"))
LEARNING_LOOKBACK = int(os.getenv("LEARNING_LOOKBACK", "100"))

# A new signal on the same symbol/feed is suppressed while the previous
# advisory is still active. This is NOT a global signal lock: other symbols
# continue normally, and TP/SL tracking continues independently.
BLOCK_SAME_FEED_WHILE_ACTIVE = os.getenv(
    "BLOCK_SAME_FEED_WHILE_ACTIVE", "1"
).strip().lower() not in {"0", "false", "no"}

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
    """Learns outcomes by broad market-state features, not exact candles."""

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
        self.data = {"version": 2, "updated_at": None, "trades": []}
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("trades"), list):
                self.data["trades"] = loaded["trades"]
                self.data["version"] = loaded.get("version", 2)
                self.data["updated_at"] = loaded.get("updated_at")
        except Exception as exc:
            log.warning("Learning data haikuweza kusomwa: %s", exc)

    def _save(self):
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    @staticmethod
    def _bucket_rr(rr):
        try:
            rr = float(rr)
        except (TypeError, ValueError):
            return "unknown"
        if rr < 1.5:
            return "<1.5"
        if rr < 2:
            return "1.5-2"
        if rr < 3:
            return "2-3"
        return "3+"

    @staticmethod
    def _norm(v):
        return "none" if v is None else str(v).strip().lower()

    def feature_key(self, f):
        # Deliberately broad: enough samples can accumulate across similar
        # setups instead of creating a new bucket for every tiny variation.
        parts = [
            self._norm(f.get("symbol")),
            self._norm(f.get("feed")),
            self._norm(f.get("direction")),
            self._norm(f.get("regime")),
            self._norm(f.get("location")),
            self._norm(f.get("sweep")),
            self._norm(f.get("setup")),
            self._norm(f.get("m15")),
            self._norm(f.get("m5")),
            self._norm(f.get("timing")),
            self._bucket_rr(f.get("rr")),
        ]
        return "|".join(parts)

    def _matches(self, key):
        return [
            x for x in self.data["trades"]
            if x.get("feature_key") == key
            and x.get("result") in ("tp", "sl")
        ][-self.lookback:]

    async def evaluate(self, features):
        async with self._lock:
            key = self.feature_key(features)
            matches = self._matches(key)
            wins = sum(x.get("result") == "tp" for x in matches)
            total = len(matches)
            rate = wins / total if total else None

            if total < self.min_samples:
                decision = "LEARN"
            elif rate >= self.min_win_rate:
                decision = "ALLOW"
            else:
                decision = "WAIT"

            return {
                "decision": decision,
                "feature_key": key,
                "samples": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": rate,
            }

    async def register_signal(self, features):
        async with self._lock:
            key = self.feature_key(features)
            self.data["trades"].append({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "feature_key": key,
                "symbol": features.get("symbol"),
                "feed": features.get("feed"),
                "direction": features.get("direction"),
                "regime": features.get("regime"),
                "location": features.get("location"),
                "sweep": features.get("sweep"),
                "setup": features.get("setup"),
                "m15": features.get("m15"),
                "m5": features.get("m5"),
                "timing": features.get("timing"),
                "rr": features.get("rr"),
                "entry": features.get("entry"),
                "tp": features.get("tp"),
                "sl": features.get("sl"),
                "entry_epoch": features.get("entry_epoch"),
                "result": None,
            })
            self.data["trades"] = self.data["trades"][-5000:]
            self._save()
            return key

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
            self._save()


def candle_range(c):
    return max(0.0, float(c["high"]) - float(c["low"]))


def market_regime(structure):
    candles = list(structure.candles)
    if len(candles) < 20:
        return "UNKNOWN"

    score_up = int(getattr(structure, "bullish_score", 0))
    score_down = int(getattr(structure, "bearish_score", 0))
    strength = getattr(structure, "structure_strength", "NEUTRAL")

    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c) > 0]
    if not ranges:
        return "UNKNOWN"

    avg = sum(ranges) / len(ranges)
    recent = candles[-6:]
    displacement = abs(recent[-1]["close"] - recent[0]["open"])

    if strength == "NEUTRAL" and displacement < avg * 1.2:
        return "RANGE"

    if score_up >= score_down + 3 and strength in ("MODERATE", "STRONG"):
        return "TREND_UP"

    if score_down >= score_up + 3 and strength in ("MODERATE", "STRONG"):
        return "TREND_DOWN"

    if displacement > avg * 2.0:
        if recent[-1]["close"] > recent[0]["open"]:
            return "EXPANSION_UP"
        return "EXPANSION_DOWN"

    return "TRANSITION"


def market_location(direction, price, structure):
    highs = [float(x) for x in list(structure.swing_highs)[-5:]]
    lows = [float(x) for x in list(structure.swing_lows)[-5:]]

    if not highs or not lows:
        return "UNKNOWN"

    high = max(highs)
    low = min(lows)
    span = high - low

    if span <= 0:
        return "UNKNOWN"

    position = (price - low) / span

    if position <= 0.35:
        return "DISCOUNT" if direction == "up" else "LOW_ZONE"
    if position >= 0.65:
        return "PREMIUM" if direction == "down" else "HIGH_ZONE"
    return "MID_RANGE"


def directional_sweep(direction, sweep):
    # Correct liquidity logic:
    # BUY needs sell-side liquidity taken (low sweep).
    # SELL needs buy-side liquidity taken (high sweep).
    return (
        (direction == "up" and sweep == "low")
        or (direction == "down" and sweep == "high")
    )


def entry_timing_ok(direction, structure):
    candles = list(structure.candles)
    if len(candles) < 10:
        return False

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    ranges = [candle_range(c) for c in candles[-10:-1] if candle_range(c)]
    if not ranges:
        return False

    avg = sum(ranges) / len(ranges)
    last_range = candle_range(c3)

    # Do not chase an abnormal expansion candle.
    if last_range > avg * 1.60:
        return False

    if direction == "up":
        pullback = (
            c2["low"] < c1["low"]
            or c2["close"] < c1["close"]
        )
        confirm = (
            c3["close"] > c3["open"]
            and c3["close"] > c2["close"]
        )
        room = max(c["high"] for c in candles[-7:-1]) - c3["close"]
    else:
        pullback = (
            c2["high"] > c1["high"]
            or c2["close"] > c1["close"]
        )
        confirm = (
            c3["close"] < c3["open"]
            and c3["close"] < c2["close"]
        )
        room = c3["close"] - min(c["low"] for c in candles[-7:-1])

    if not (pullback and confirm):
        return False

    if room < avg * 0.50:
        return False

    return True


def calculate_levels(direction, entry, structure, symbol, feed_label):
    # Use the LTF structure for invalidation, not M15.
    candles = list(structure.candles)
    highs = [float(x) for x in list(structure.swing_highs)[-6:]]
    lows = [float(x) for x in list(structure.swing_lows)[-6:]]

    if len(candles) < 10:
        return None, None

    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c)]
    if not ranges:
        return None, None
    avg = sum(ranges) / len(ranges)

    minimum = (
        BROKER_MIN_POINTS.get((symbol, feed_label), 0)
        * BROKER_POINT_SIZE
    )
    minimum *= 1.05

    if direction == "up":
        supports = [x for x in lows if x < entry]
        if not supports:
            return None, None

        sl = max(supports[-3:]) - avg * 0.20
        sl = min(sl, entry - avg * 0.55, entry - minimum)
        risk = entry - sl
        if risk <= 0:
            return None, None

        targets = [x for x in highs if x > entry]
        min_reward = max(minimum, risk * MIN_RR_RATIO)
        target = None
        for level in sorted(targets):
            if level - entry >= min_reward:
                target = level
                break
        tp = target if target is not None else entry + min_reward
        return sl, tp

    resistances = [x for x in highs if x > entry]
    if not resistances:
        return None, None

    sl = min(resistances[-3:]) + avg * 0.20
    sl = max(sl, entry + avg * 0.55, entry + minimum)
    risk = sl - entry
    if risk <= 0:
        return None, None

    targets = [x for x in lows if x < entry]
    min_reward = max(minimum, risk * MIN_RR_RATIO)
    target = None
    for level in sorted(targets, reverse=True):
        if entry - level >= min_reward:
            target = level
            break
    tp = target if target is not None else entry - min_reward
    return sl, tp


class TimeframeBuilder:
    def __init__(self, seconds):
        self.seconds = seconds
        self.current = None

    def update(self, candle):
        epoch = int(candle["epoch"])
        bucket = epoch - epoch % self.seconds

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
            self.current["high"] = max(self.current["high"], candle["high"])
            self.current["low"] = min(self.current["low"], candle["low"])
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
        learner,
    ):
        self.symbol = symbol
        self.display_name = display_name
        self.feed_label = feed_label
        self.point_symbol = point_symbol
        self.telegram = telegram
        self.tracker = tracker
        self.memory = memory
        self.learner = learner

        self.htf = SMCAnalyzer(symbol, lookback=2, history=300)
        self.mtf = SMCAnalyzer(symbol, lookback=2, history=300)
        self.ltf = SMCAnalyzer(symbol, lookback=2, history=300)

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
        try:
            htf_data = await client.get_candles(
                self.symbol, granularity=900, count=CANDLE_COUNT
            )
            mtf_data = await client.get_candles(
                self.symbol, granularity=300, count=CANDLE_COUNT
            )
            ltf_data = await client.get_candles(
                self.symbol, granularity=60, count=CANDLE_COUNT
            )

            for candle in htf_data:
                self.htf.add_candle(clean_candle(candle))
            for candle in mtf_data:
                self.mtf.add_candle(clean_candle(candle))
            for candle in ltf_data:
                c = clean_candle(candle)
                self.ltf_closes.append(c["close"])
                self.ltf.add_candle(c)

            # Start builders from the latest completed historical candle.
            if mtf_data:
                self.mtf_builder.current = clean_candle(mtf_data[-1])
            if htf_data:
                self.htf_builder.current = clean_candle(htf_data[-1])
            if ltf_data:
                self.ltf_builder.current = clean_candle(ltf_data[-1])

            self.ready = True

            log.info(
                "[%s | %s] Ready M15=%s M5=%s M1=%s",
                self.display_name,
                self.feed_label,
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
        if not self.tracker.is_active(self.symbol, self.feed_label):
            return

        completed = self.tracker.check_price(
            self.symbol, self.feed_label, price
        )
        if completed is None:
            return

        event_id = self.active_event_id
        learning_key = self.active_learning_key
        self.active_event_id = None
        self.active_learning_key = None

        result = completed["result"].lower()

        if event_id:
            self.memory.record_result(
                self.symbol,
                event_id,
                result,
                exit_price=completed["exit"],
                exit_epoch=epoch,
            )

        if learning_key:
            await self.learner.register_result(
                learning_key,
                result,
                exit_price=completed["exit"],
                exit_epoch=epoch,
            )

        icon = "✅" if result == "tp" else "🛑"
        message = (
            f"{icon} <b>TRADE IMEKAMILIKA</b>\n\n"
            f"📡 Feed: <b>{self.feed_label}</b>\n"
            f"📌 Deriv: <b>{self.symbol}</b>\n"
            f"📍 MT5: <b>{self.display_name}</b>\n"
            f"📊 Result: <b>{result.upper()}</b>\n"
            f"💰 Entry: <b>{completed['entry']:.4f}</b>\n"
            f"🏁 Exit: <b>{completed['exit']:.4f}</b>\n"
            f"🎯 TP: <b>{completed['tp']:.4f}</b>\n"
            f"🛑 SL: <b>{completed['sl']:.4f}</b>\n"
            f"⏱️ Duration: <b>{completed['duration_seconds']:.0f}s</b>\n\n"
            "🔓 Signal mpya ya symbol hii inaweza kuchambuliwa."
        )

        try:
            await self.telegram.send(message)
        except Exception as exc:
            log.exception("Result Telegram error: %s", exc)

    async def on_candle(self, symbol, candle):
        if symbol != self.symbol or not self.ready:
            return
        if int(candle.get("granularity", 60)) != 60:
            return

        c = clean_candle(candle)

        # Tick-level TP/SL tracking remains independent from signal generation.
        await self._track_active_trade(
            c["close"],
            int(candle.get("tick_epoch", c["epoch"])),
        )

        completed_m1 = self.ltf_builder.update(c)
        if completed_m1 is None:
            return

        self.ltf_closes.append(completed_m1["close"])
        ltf_setup = self.ltf.add_candle(completed_m1)

        completed_m5 = self.mtf_builder.update(completed_m1)
        if completed_m5:
            self.mtf.add_candle(completed_m5)

        completed_m15 = self.htf_builder.update(completed_m1)
        if completed_m15:
            self.htf.add_candle(completed_m15)

        if ltf_setup:
            await self.evaluate_signal(ltf_setup, completed_m1)

    async def evaluate_signal(self, ltf_setup, candle):
        now = time.time()

        # This is NOT a global lock. It only protects a trader from receiving
        # a contradictory new advisory on the same symbol/feed while the
        # previous advisory is still active.
        if (
            BLOCK_SAME_FEED_WHILE_ACTIVE
            and self.tracker.is_active(self.symbol, self.feed_label)
        ):
            return

        if now - self.last_signal_time < MIN_SECONDS_BETWEEN_SIGNALS:
            return

        direction = ltf_setup.get("direction")
        if direction not in ("up", "down"):
            return

        if self.htf.trend != direction or self.mtf.trend != direction:
            return

        if (
            self.htf.structure_strength == "NEUTRAL"
            or self.mtf.structure_strength == "NEUTRAL"
        ):
            return

        regime = market_regime(self.mtf)
        if direction == "up" and regime not in (
            "TREND_UP", "EXPANSION_UP", "TRANSITION"
        ):
            return
        if direction == "down" and regime not in (
            "TREND_DOWN", "EXPANSION_DOWN", "TRANSITION"
        ):
            return

        price = float(candle["close"])
        location = market_location(direction, price, self.mtf)

        # Do not buy at premium or sell at discount.
        if direction == "up" and location in ("PREMIUM", "MID_RANGE", "HIGH_ZONE"):
            return
        if direction == "down" and location in ("DISCOUNT", "MID_RANGE", "LOW_ZONE"):
            return

        sweep = ltf_setup.get("sweep")
        if not directional_sweep(direction, sweep):
            return

        if not entry_timing_ok(direction, self.ltf):
            return

        sl, tp = calculate_levels(
            direction,
            price,
            self.ltf,
            self.symbol,
            self.feed_label,
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

        timing = "PULLBACK_CONFIRMATION"

        features = {
            "symbol": self.symbol,
            "feed": self.feed_label,
            "direction": direction,
            "regime": regime,
            "location": location,
            "sweep": sweep,
            "setup": ltf_setup.get("reason", "SMC"),
            "m15": self.htf.trend,
            "m5": self.mtf.trend,
            "timing": timing,
            "rr": rr,
        }

        learning = await self.learner.evaluate(features)
        if learning["decision"] == "WAIT":
            log.info(
                "[%s | %s] LEARNING WAIT samples=%s winrate=%s",
                self.display_name,
                self.feed_label,
                learning["samples"],
                learning["win_rate"],
            )
            return

        rsi_value = rsi(self.ltf_closes, RSI_PERIOD)
        sma_value = sma(self.ltf_closes, SMA_TREND)

        # RSI/SMA are confirmations, not the primary direction engine.
        if rsi_value is not None:
            if direction == "up" and rsi_value >= 78:
                return
            if direction == "down" and rsi_value <= 22:
                return

        if sma_value is not None:
            if direction == "up" and price < sma_value:
                return
            if direction == "down" and price > sma_value:
                return

        strength_ok = (
            self.htf.structure_strength == "STRONG"
            and self.mtf.structure_strength == "STRONG"
        )
        confidence = "HIGH" if strength_ok else "GOOD"

        lot = None
        if self.point_value and self.point_value > 0:
            risk_money = ACCOUNT_BALANCE * (
                RISK_PERCENT_PER_TRADE / 100
            )
            lot = max(
                round(risk_money / (risk * self.point_value), 2),
                0.01,
            )

        action = "NUNUA (BUY)" if direction == "up" else "UZA (SELL)"
        icon = "📈" if direction == "up" else "📉"
        sweep_text = (
            "SSL swept + bullish reaction"
            if direction == "up"
            else "BSL swept + bearish reaction"
        )
        learning_text = (
            f"{learning['decision']} | "
            f"samples={learning['samples']}"
            if learning["decision"] != "LEARN"
            else "LEARNING: bado inakusanya data"
        )

        message = (
            f"{icon} <b>ISHARA: {action}</b>\n\n"
            f"📡 Feed: <b>{self.feed_label}</b>\n"
            f"📌 Deriv: <b>{self.symbol}</b>\n"
            f"📍 MT5: <b>{self.display_name}</b>\n"
            f"🎯 Confidence: <b>{confidence}</b>\n"
            f"💰 Entry: <b>{price:.4f}</b>\n"
            f"🎯 TP: <b>{tp:.4f}</b>\n"
            f"🛑 SL: <b>{sl:.4f}</b>\n"
            f"⚖️ R:R: <b>1:{rr:.2f}</b>\n"
            f"📊 Lot: <b>{lot if lot is not None else 'N/A'}</b>\n\n"
            f"🧠 M15: <b>{self.htf.trend.upper()}</b> "
            f"({self.htf.structure_strength})\n"
            f"🔄 M5: <b>{self.mtf.trend.upper()}</b> "
            f"({self.mtf.structure_strength})\n"
            f"⚡ M1: <b>{direction.upper()}</b>\n"
            f"🌐 Regime: <b>{regime}</b>\n"
            f"📍 Location: <b>{location}</b>\n"
            f"💧 Liquidity: <b>{sweep_text}</b>\n"
            f"⏱️ Timing: <b>{timing}</b>\n"
            f"📊 RSI: <b>{rsi_value:.1f}</b>\n"
            f"📏 SMA: <b>{'JUU' if sma_value and price >= sma_value else 'CHINI'}</b>\n"
            f"🧠 Learning: <b>{learning_text}</b>\n\n"
            "⚠️ <i>Advisory only. Hakuna order inayowekwa na bot.</i>"
        )

        try:
            await self.telegram.send(message)

            signal_data = {
                "direction": direction,
                "entry": price,
                "tp": tp,
                "sl": sl,
                "rr": rr,
                "setup": ltf_setup.get("reason", "SMC"),
                "sweep": sweep,
                "confidence": confidence,
                "m15": self.htf.trend,
                "m5": self.mtf.trend,
                "m1": direction,
                "rsi": rsi_value,
                "sma": (
                    "JUU"
                    if sma_value is not None and price >= sma_value
                    else "CHINI"
                ),
                "entry_epoch": int(candle.get("epoch", time.time())),
                "regime": regime,
                "location": location,
                "timing": timing,
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
                direction,
                price,
                tp,
                sl,
                self.display_name,
            )

            if not registered:
                log.warning(
                    "[%s | %s] Tracker rejected after Telegram send.",
                    self.display_name,
                    self.feed_label,
                )
                return

            self.active_event_id = event_id
            self.active_learning_key = await self.learner.register_signal(
                features | {
                    "entry": price,
                    "tp": tp,
                    "sl": sl,
                    "entry_epoch": candle.get("epoch"),
                }
            )
            self.last_signal_time = now
            self.last_signal_direction = direction

            log.info(
                "[%s | %s] SIGNAL %s entry=%.4f sl=%.4f tp=%.4f rr=%.2f",
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
                "[%s | %s] Telegram/signal error: %s",
                self.display_name,
                self.feed_label,
                exc,
            )


async def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN na TELEGRAM_CHAT_ID lazima ziwekwe."
        )

    telegram = TelegramNotifier(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )
    tracker = TradeTracker()
    memory = SymbolMemory()
    learner = AdaptiveLearningEngine()

    client = PublicMarketClient(timeout=20)
    await client.connect()

    monitors = [
        PairMonitor(
            deriv_symbol,
            display_name,
            feed_label,
            point_symbol,
            telegram,
            tracker,
            memory,
            learner,
        )
        for deriv_symbol, display_name, feed_label, point_symbol in SYMBOLS
    ]

    for monitor in monitors:
        await monitor.initialize(client)

    async def callback(symbol, candle):
        for monitor in monitors:
            if monitor.symbol == symbol:
                await monitor.on_candle(symbol, candle)
                return

    client.on_candle = callback

    for monitor in monitors:
        try:
            await client.subscribe_candles(
                monitor.symbol,
                granularity=60,
            )
            log.info(
                "[%s | %s] Live M1 stream started.",
                monitor.display_name,
                monitor.feed_label,
            )
            await asyncio.sleep(0.5)
        except Exception as exc:
            log.exception(
                "[%s | %s] Stream start error: %s",
                monitor.display_name,
                monitor.feed_label,
                exc,
            )

    await telegram.send(
        "🤖 <b>Signal Advisory Engine</b>\n\n"
        "🧠 Direction: M15 + M5 + M1\n"
        "🌐 Market regime + location\n"
        "💧 Directional liquidity sweep\n"
        "⏱️ Pullback + confirmation timing\n"
        "🛡️ LTF invalidation / SL\n"
        "🧠 Adaptive feedback learning\n"
        "🔒 Same symbol/feed conflict protection: ACTIVE\n"
        "🔓 Global signal lock: OFF\n\n"
        "⚠️ Advisory only — bot hai-trade."
    )

    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
