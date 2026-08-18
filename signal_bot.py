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
from market_movement import MovementEngine

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
MIN_SECONDS_BETWEEN_SIGNALS = int(os.getenv("MIN_SECONDS_BETWEEN_SIGNALS", "900"))
QUALITY_THRESHOLD = int(os.getenv("SIGNAL_QUALITY_THRESHOLD", "72"))
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PERCENT_PER_TRADE = float(os.getenv("RISK_PERCENT_PER_TRADE", "1"))

LEARNING_FILE = os.getenv("LEARNING_FILE", "learning_data.json").strip()
LEARNING_MIN_SAMPLES = int(os.getenv("LEARNING_MIN_SAMPLES", "8"))
LEARNING_MIN_WIN_RATE = float(os.getenv("LEARNING_MIN_WIN_RATE", "0.52"))
LEARNING_LOOKBACK = int(os.getenv("LEARNING_LOOKBACK", "100"))

BLOCK_SAME_FEED_WHILE_ACTIVE = os.getenv(
    "BLOCK_SAME_FEED_WHILE_ACTIVE", "1"
).strip().lower() not in {"0", "false", "no"}

BROKER_MIN_POINTS = {
    ("R_10", "2s"): 720, ("R_25", "2s"): 423,
    ("R_50", "2s"): 1350, ("R_75", "2s"): 10770,
    ("R_100", "2s"): 138, ("1HZ10V", "1s"): 106,
    ("1HZ25V", "1s"): 10215, ("1HZ50V", "1s"): 6996,
    ("1HZ75V", "1s"): 432, ("1HZ100V", "1s"): 72,
}
BROKER_POINT_SIZE = 0.01

POINT_VALUES = {}
for item in os.getenv(
    "POINT_VALUES", "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1"
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
        "epoch": int(candle["epoch"]), "open": float(candle["open"]),
        "high": float(candle["high"]), "low": float(candle["low"]),
        "close": float(candle["close"]),
        "granularity": int(candle.get("granularity", 60)),
    }


def candle_range(candle):
    return max(0.0, float(candle["high"]) - float(candle["low"]))


def market_regime(structure):
    candles = list(structure.candles)
    if len(candles) < 20:
        return "UNKNOWN"
    up = int(getattr(structure, "bullish_score", 0))
    down = int(getattr(structure, "bearish_score", 0))
    strength = getattr(structure, "structure_strength", "NEUTRAL")
    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c) > 0]
    if not ranges:
        return "UNKNOWN"
    avg = sum(ranges) / len(ranges)
    recent = candles[-6:]
    displacement = abs(recent[-1]["close"] - recent[0]["open"])
    if up >= down + 3 and strength in ("MODERATE", "STRONG"):
        return "TREND_UP"
    if down >= up + 3 and strength in ("MODERATE", "STRONG"):
        return "TREND_DOWN"
    if displacement >= avg * 2.0:
        return "EXPANSION_UP" if recent[-1]["close"] > recent[0]["open"] else "EXPANSION_DOWN"
    if strength == "NEUTRAL" and displacement < avg * 1.2:
        return "RANGE"
    return "TRANSITION"


def market_location(direction, price, structure):
    highs = [float(x) for x in list(structure.swing_highs)[-5:]]
    lows = [float(x) for x in list(structure.swing_lows)[-5:]]
    if not highs or not lows:
        return "UNKNOWN"
    high, low = max(highs), min(lows)
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
    return (direction == "up" and sweep == "low") or (direction == "down" and sweep == "high")


def calculate_levels(direction, entry, structure, symbol, feed_label):
    candles = list(structure.candles)
    highs = [float(x) for x in list(structure.swing_highs)[-6:]]
    lows = [float(x) for x in list(structure.swing_lows)[-6:]]
    if len(candles) < 10:
        return None, None
    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c) > 0]
    if not ranges:
        return None, None
    avg = sum(ranges) / len(ranges)
    minimum = BROKER_MIN_POINTS.get((symbol, feed_label), 0) * BROKER_POINT_SIZE * 1.05

    if direction == "up":
        supports = [x for x in lows if x < entry]
        if not supports:
            return None, None
        sl = max(supports[-3:]) - avg * 0.20
        sl = min(sl, entry - avg * 0.55, entry - minimum)
        risk = entry - sl
        if risk <= 0:
            return None, None
        min_reward = max(minimum, risk * MIN_RR_RATIO)
        targets = [x for x in highs if x > entry]
        target = next((x for x in sorted(targets) if x - entry >= min_reward), None)
        return sl, target if target is not None else entry + min_reward

    resistances = [x for x in highs if x > entry]
    if not resistances:
        return None, None
    sl = min(resistances[-3:]) + avg * 0.20
    sl = max(sl, entry + avg * 0.55, entry + minimum)
    risk = sl - entry
    if risk <= 0:
        return None, None
    min_reward = max(minimum, risk * MIN_RR_RATIO)
    targets = [x for x in lows if x < entry]
    target = next((x for x in sorted(targets, reverse=True) if entry - x >= min_reward), None)
    return sl, target if target is not None else entry - min_reward


class TimeframeBuilder:
    def __init__(self, seconds):
        self.seconds = int(seconds)
        self.current = None

    def update(self, candle):
        epoch = int(candle["epoch"])
        bucket = epoch - epoch % self.seconds
        if self.current is None:
            self.current = {"epoch": bucket, "open": candle["open"], "high": candle["high"], "low": candle["low"], "close": candle["close"], "granularity": self.seconds}
            return None
        if bucket == self.current["epoch"]:
            self.current["high"] = max(self.current["high"], candle["high"])
            self.current["low"] = min(self.current["low"], candle["low"])
            self.current["close"] = candle["close"]
            return None
        completed = dict(self.current)
        self.current = {"epoch": bucket, "open": candle["open"], "high": candle["high"], "low": candle["low"], "close": candle["close"], "granularity": self.seconds}
        return completed


class AdaptiveLearningEngine:
    def __init__(self, path=LEARNING_FILE, min_samples=LEARNING_MIN_SAMPLES, min_win_rate=LEARNING_MIN_WIN_RATE, lookback=LEARNING_LOOKBACK):
        self.path = path
        self.min_samples = max(1, int(min_samples))
        self.min_win_rate = float(min_win_rate)
        self.lookback = max(20, int(lookback))
        self._lock = asyncio.Lock()
        self.data = {"version": 3, "updated_at": None, "trades": []}
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict) and isinstance(loaded.get("trades"), list):
                self.data["trades"] = loaded["trades"]
        except Exception as exc:
            log.warning("Learning data error: %s", exc)

    def _save(self):
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    @staticmethod
    def _norm(value):
        return "none" if value is None else str(value).strip().lower()

    @staticmethod
    def _bucket_rr(rr):
        try:
            value = float(rr)
        except (TypeError, ValueError):
            return "unknown"
        if value < 1.5:
            return "<1.5"
        if value < 2:
            return "1.5-2"
        if value < 3:
            return "2-3"
        return "3+"

    def feature_key(self, features):
        return "|".join([
            self._norm(features.get("symbol")), self._norm(features.get("feed")),
            self._norm(features.get("direction")), self._norm(features.get("regime")),
            self._norm(features.get("location")), self._norm(features.get("sweep")),
            self._norm(features.get("setup")), self._norm(features.get("d1")),
            self._norm(features.get("h4")), self._norm(features.get("m30")),
            self._norm(features.get("m15")), self._norm(features.get("m5")),
            self._norm(features.get("movement_direction")), self._norm(features.get("volatility")),
            self._norm(features.get("movement_state")), self._bucket_rr(features.get("rr")),
        ])

    def _matches(self, key):
        return [x for x in self.data["trades"] if x.get("feature_key") == key and x.get("result") in ("tp", "sl")][-self.lookback:]

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
            return {"decision": decision, "feature_key": key, "samples": total, "wins": wins, "losses": total - wins, "win_rate": rate}

    async def register_signal(self, features):
        async with self._lock:
            key = self.feature_key(features)
            fields = ("symbol", "feed", "direction", "regime", "location", "sweep", "setup", "d1", "h4", "m30", "m15", "m5", "movement_direction", "volatility", "movement_state", "movement_score", "movement_pressure", "rr", "entry", "tp", "sl", "entry_epoch")
            self.data["trades"].append({"created_at": datetime.now(timezone.utc).isoformat(), "feature_key": key, **{k: features.get(k) for k in fields}, "result": None})
            self.data["trades"] = self.data["trades"][-5000:]
            self._save()
            return key

    async def register_result(self, feature_key, result, exit_price=None, exit_epoch=None):
        result = str(result).lower()
        if result not in ("tp", "sl"):
            return
        async with self._lock:
            for item in reversed(self.data["trades"]):
                if item.get("feature_key") == feature_key and item.get("result") is None:
                    item["result"] = result
                    item["exit"] = exit_price
                    item["exit_epoch"] = exit_epoch
                    item["completed_at"] = datetime.now(timezone.utc).isoformat()
                    break
            self._save()


class PairMonitor:
    def __init__(self, symbol, display_name, feed_label, point_symbol, telegram, tracker, memory, learner):
        self.symbol = symbol
        self.display_name = display_name
        self.feed_label = feed_label
        self.point_symbol = point_symbol
        self.telegram = telegram
        self.tracker = tracker
        self.memory = memory
        self.learner = learner

        self.d1 = SMCAnalyzer(symbol, lookback=2, history=300)
        self.h4 = SMCAnalyzer(symbol, lookback=2, history=300)
        self.m30 = SMCAnalyzer(symbol, lookback=2, history=300)
        self.m15 = SMCAnalyzer(symbol, lookback=2, history=300)
        self.m5 = SMCAnalyzer(symbol, lookback=2, history=300)
        self.m1 = SMCAnalyzer(symbol, lookback=2, history=300)

        self.b30 = TimeframeBuilder(1800)
        self.b4h = TimeframeBuilder(14400)
        self.b1d = TimeframeBuilder(86400)
        self.b15 = TimeframeBuilder(900)
        self.b5 = TimeframeBuilder(300)
        self.b1 = TimeframeBuilder(60)

        self.ltf_closes = deque(maxlen=100)
        self.movement = MovementEngine()
        self.last_signal_time = 0
        self.active_event_id = None
        self.active_learning_key = None
        self.ready = False
        self.point_value = POINT_VALUES.get(point_symbol)

    async def initialize(self, client):
        try:
            histories = {
                "d1": await client.get_candles(self.symbol, granularity=86400, count=CANDLE_COUNT),
                "h4": await client.get_candles(self.symbol, granularity=14400, count=CANDLE_COUNT),
                "m30": await client.get_candles(self.symbol, granularity=1800, count=CANDLE_COUNT),
                "m15": await client.get_candles(self.symbol, granularity=900, count=CANDLE_COUNT),
                "m5": await client.get_candles(self.symbol, granularity=300, count=CANDLE_COUNT),
                "m1": await client.get_candles(self.symbol, granularity=60, count=CANDLE_COUNT),
            }
            for c in histories["d1"]:
                self.d1.add_candle(clean_candle(c))
            for c in histories["h4"]:
                self.h4.add_candle(clean_candle(c))
            for c in histories["m30"]:
                self.m30.add_candle(clean_candle(c))
            for c in histories["m15"]:
                self.m15.add_candle(clean_candle(c))
            for c in histories["m5"]:
                self.m5.add_candle(clean_candle(c))
            for c in histories["m1"]:
                cc = clean_candle(c)
                self.ltf_closes.append(cc["close"])
                self.m1.add_candle(cc)
                self.movement.update_tick(cc["close"], cc["epoch"], cc)

            for key, builder in (("d1", self.b1d), ("h4", self.b4h), ("m30", self.b30), ("m15", self.b15), ("m5", self.b5), ("m1", self.b1)):
                if histories[key]:
                    builder.current = clean_candle(histories[key][-1])

            self.ready = True
            log.info(
                "[%s | %s] READY | 1D=%s 4H=%s 30M=%s 15M=%s 5M=%s 1M=%s",
                self.display_name, self.feed_label, self.d1.trend, self.h4.trend,
                self.m30.trend, self.m15.trend, self.m5.trend, self.m1.trend,
            )
        except Exception as exc:
            log.exception("[%s | %s] History error: %s", self.display_name, self.feed_label, exc)

    async def _track_active_trade(self, price, epoch):
        if not self.tracker.is_active(self.symbol, self.feed_label):
            return
        completed = self.tracker.check_price(self.symbol, self.feed_label, price)
        if completed is None:
            return
        event_id = self.active_event_id
        learning_key = self.active_learning_key
        self.active_event_id = None
        self.active_learning_key = None
        result = completed["result"].lower()
        if event_id:
            self.memory.record_result(self.symbol, event_id, result, exit_price=completed["exit"], exit_epoch=epoch)
        if learning_key:
            await self.learner.register_result(learning_key, result, exit_price=completed["exit"], exit_epoch=epoch)
        icon = "✅" if result == "tp" else "🛑"
        try:
            await self.telegram.send(
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
                "🧠 Result imeingia kwenye feedback engine."
            )
        except Exception as exc:
            log.exception("Result Telegram error: %s", exc)

    async def on_candle(self, symbol, candle):
        if symbol != self.symbol or not self.ready:
            return
        if int(candle.get("granularity", 60)) != 60:
            return
        c = clean_candle(candle)
        tick_epoch = int(candle.get("tick_epoch", c["epoch"]))
        movement = self.movement.update_tick(c["close"], tick_epoch, c)
        await self._track_active_trade(c["close"], tick_epoch)

        completed_m1 = self.b1.update(c)
        if completed_m1 is None:
            return
        self.ltf_closes.append(completed_m1["close"])
        ltf_setup = self.m1.add_candle(completed_m1)

        completed_m5 = self.b5.update(completed_m1)
        if completed_m5:
            self.m5.add_candle(completed_m5)
        completed_m15 = self.b15.update(completed_m1)
        if completed_m15:
            self.m15.add_candle(completed_m15)
        completed_m30 = self.b30.update(completed_m1)
        if completed_m30:
            self.m30.add_candle(completed_m30)
        completed_h4 = self.b4h.update(completed_m1)
        if completed_h4:
            self.h4.add_candle(completed_h4)
        completed_d1 = self.b1d.update(completed_m1)
        if completed_d1:
            self.d1.add_candle(completed_d1)

        if ltf_setup:
            await self.evaluate_signal(ltf_setup, completed_m1, movement)

    async def evaluate_signal(self, ltf_setup, candle, movement):
        now = time.time()
        if BLOCK_SAME_FEED_WHILE_ACTIVE and self.tracker.is_active(self.symbol, self.feed_label):
            return
        if now - self.last_signal_time < MIN_SECONDS_BETWEEN_SIGNALS:
            return

        direction = ltf_setup.get("direction")
        if direction not in ("up", "down"):
            return

        if self.m15.trend != direction or self.m5.trend != direction:
            return
        if self.m15.structure_strength == "NEUTRAL" or self.m5.structure_strength == "NEUTRAL":
            return

        price = float(candle["close"])
        regime = market_regime(self.m5)
        if direction == "up" and regime == "TREND_DOWN":
            return
        if direction == "down" and regime == "TREND_UP":
            return

        location = market_location(direction, price, self.m5)
        if direction == "up" and location == "PREMIUM":
            return
        if direction == "down" and location == "DISCOUNT":
            return

        sweep = ltf_setup.get("sweep")
        sweep_pass = directional_sweep(direction, sweep)
        rsi_value = rsi(self.ltf_closes, RSI_PERIOD)
        sma_value = sma(self.ltf_closes, SMA_TREND)

        rsi_pass = True
        if rsi_value is not None:
            rsi_pass = rsi_value < 78 if direction == "up" else rsi_value > 22
        sma_pass = True
        if sma_value is not None:
            sma_pass = price >= sma_value if direction == "up" else price <= sma_value

        movement_direction = movement.get("direction")
        movement_score = int(movement.get("score", 0))
        volatility = movement.get("volatility", "UNKNOWN")
        rejection = movement.get("rejection", "NONE")
        movement_aligned = (
            movement_direction == direction
            or (rejection == "LOW_REJECTION" and direction == "up")
            or (rejection == "HIGH_REJECTION" and direction == "down")
        )

        if volatility == "LOW" or not movement_aligned:
            return

        sl, tp = calculate_levels(direction, price, self.m1, self.symbol, self.feed_label)
        if sl is None or tp is None:
            return
        risk = abs(price - sl)
        reward = abs(tp - price)
        if risk <= 0:
            return
        rr = reward / risk
        if rr < MIN_RR_RATIO:
            return

        quality = 0
        reasons = []
        if self.m15.trend == direction:
            quality += 15
            reasons.append("M15")
        if self.m5.trend == direction:
            quality += 15
            reasons.append("M5")
        if self.m15.structure_strength == "STRONG":
            quality += 5
        if self.m5.structure_strength == "STRONG":
            quality += 5
        if sweep_pass:
            quality += 15
            reasons.append("LIQUIDITY")
        if movement_aligned:
            quality += 15
            reasons.append("MOMENTUM")
        if volatility == "EXPANDING":
            quality += 10
            reasons.append("VOL_EXPANSION")
        elif volatility == "NORMAL":
            quality += 5
        if movement.get("candle_body_ratio", 0) >= 0.55:
            quality += 5
        if direction == "up" and location == "DISCOUNT":
            quality += 5
        elif direction == "down" and location == "PREMIUM":
            quality += 5
        if rsi_pass:
            quality += 3
        if sma_pass:
            quality += 3
        if rr >= 2:
            quality += 4

        higher_context = []
        for label, analyzer, points in (("1D", self.d1, 8), ("4H", self.h4, 7), ("30M", self.m30, 6)):
            if analyzer.trend == direction:
                quality += points
                higher_context.append(f"{label} {direction.upper()}")
            elif analyzer.trend not in (None, direction):
                quality -= 4

        quality = max(0, min(100, quality))
        if quality < QUALITY_THRESHOLD:
            log.info(
                "[%s | %s] QUALITY WAIT dir=%s score=%s/%s vol=%s movement=%s sweep=%s regime=%s location=%s",
                self.display_name, self.feed_label, direction, quality, QUALITY_THRESHOLD,
                volatility, movement_direction, sweep, regime, location,
            )
            return

        features = {
            "symbol": self.symbol, "feed": self.feed_label, "direction": direction,
            "regime": regime, "location": location, "sweep": sweep,
            "setup": ltf_setup.get("reason", "SMC"),
            "d1": self.d1.trend, "h4": self.h4.trend, "m30": self.m30.trend,
            "m15": self.m15.trend, "m5": self.m5.trend,
            "movement_direction": movement_direction, "volatility": volatility,
            "movement_state": "ALIGNED" if movement_aligned else "CONFLICT",
            "movement_score": movement_score, "movement_pressure": movement.get("pressure"),
            "rr": rr,
        }
        learning = await self.learner.evaluate(features)
        if learning["decision"] == "WAIT":
            log.info(
                "[%s | %s] LEARNING WAIT samples=%s winrate=%s",
                self.display_name, self.feed_label, learning["samples"], learning["win_rate"],
            )
            return

        confidence = "VERY HIGH" if quality >= 88 else "HIGH" if quality >= 80 else "GOOD"
        lot = None
        if self.point_value and self.point_value > 0:
            risk_money = ACCOUNT_BALANCE * (RISK_PERCENT_PER_TRADE / 100)
            lot = max(round(risk_money / (risk * self.point_value), 2), 0.01)

        action = "NUNUA (BUY)" if direction == "up" else "UZA (SELL)"
        icon = "📈" if direction == "up" else "📉"
        sweep_text = "SSL swept — sell-side liquidity taken" if sweep == "low" else "BSL swept — buy-side liquidity taken" if sweep == "high" else "Hakuna sweep"

        def tf_text(analyzer):
            return f"{(analyzer.trend or 'N/A').upper()} ({analyzer.structure_strength or 'N/A'})"

        message = (
            f"{icon} <b>ADVISORY SIGNAL: {action}</b>\n\n"
            f"📡 Feed: <b>{self.feed_label}</b>\n"
            f"📌 Deriv: <b>{self.symbol}</b>\n"
            f"📍 MT5: <b>{self.display_name}</b>\n"
            f"⭐ Quality: <b>{quality}/100 — {confidence}</b>\n\n"
            f"💰 Entry: <b>{price:.4f}</b>\n"
            f"🎯 TP: <b>{tp:.4f}</b>\n"
            f"🛑 SL: <b>{sl:.4f}</b>\n"
            f"⚖️ R:R: <b>1:{rr:.2f}</b>\n"
            f"📊 Lot: <b>{lot if lot else 'N/A'}</b>\n\n"
            f"🌍 1D: <b>{tf_text(self.d1)}</b>\n"
            f"🕓 4H: <b>{tf_text(self.h4)}</b>\n"
            f"🕧 30M: <b>{tf_text(self.m30)}</b>\n"
            f"🧠 15M: <b>{tf_text(self.m15)}</b>\n"
            f"🔄 5M: <b>{tf_text(self.m5)}</b>\n"
            f"⚡ 1M: <b>{direction.upper()}</b>\n\n"
            f"🌐 Regime: <b>{regime}</b>\n"
            f"📍 Location: <b>{location}</b>\n"
            f"💧 Liquidity: <b>{sweep_text}</b>\n"
            f"🚀 Movement: <b>{movement_direction or 'N/A'}</b>\n"
            f"🔥 Volatility: <b>{volatility}</b> ({movement.get('range_ratio', 0):.2f}x)\n"
            f"📈 Momentum score: <b>{movement_score}/100</b>\n"
            f"🕯️ Body: <b>{movement.get('candle_body_ratio', 0):.0%}</b>\n"
            f"⏱️ Timing: <b>PULLBACK + LIVE CONFIRMATION</b>\n"
            f"🧠 Feedback: <b>{learning['decision']}</b> ({learning['samples']} samples)\n\n"
            f"🔎 Context: <b>{', '.join(higher_context) or 'mixed'}</b>\n"
            f"🧩 Reasons: <b>{', '.join(reasons)}</b>\n\n"
            "⚠️ <i>Advisory only. Bot hai-trade.</i>"
        )

        try:
            await self.telegram.send(message)
            signal_data = {
                **features, "confidence": confidence, "quality": quality,
                "entry": price, "tp": tp, "sl": sl, "rsi": rsi_value,
                "sma": "JUU" if sma_value is not None and price >= sma_value else "CHINI",
                "entry_epoch": int(candle.get("epoch", time.time())),
            }
            event_id = self.memory.record_signal(self.symbol, self.feed_label, self.display_name, signal_data)
            registered = self.tracker.register(self.symbol, self.feed_label, direction, price, tp, sl, self.display_name)
            if not registered:
                log.warning("[%s | %s] Tracker rejected.", self.display_name, self.feed_label)
                return
            self.active_event_id = event_id
            self.active_learning_key = await self.learner.register_signal(signal_data)
            self.last_signal_time = now
            log.info(
                "[%s | %s] SIGNAL %s quality=%s entry=%.4f sl=%.4f tp=%.4f rr=%.2f",
                self.display_name, self.feed_label, action, quality, price, sl, tp, rr,
            )
        except Exception as exc:
            log.exception("[%s | %s] Telegram/signal error: %s", self.display_name, self.feed_label, exc)


async def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN na TELEGRAM_CHAT_ID lazima ziwekwe.")

    telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    tracker = TradeTracker()
    memory = SymbolMemory()
    learner = AdaptiveLearningEngine()
    client = PublicMarketClient(timeout=20)
    await client.connect()

    monitors = [
        PairMonitor(deriv_symbol, display_name, feed_label, point_symbol, telegram, tracker, memory, learner)
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
            await client.subscribe_candles(monitor.symbol, granularity=60)
            log.info("[%s | %s] Live tick/M1 stream started.", monitor.display_name, monitor.feed_label)
            await asyncio.sleep(0.5)
        except Exception as exc:
            log.exception("[%s | %s] Stream start error: %s", monitor.display_name, monitor.feed_label, exc)

    await telegram.send(
        "🤖 <b>Volatility Advisory Engine v8</b>\n\n"
        "🌍 1D + 4H + 30M: long-movement context\n"
        "🧠 15M + 5M: directional structure\n"
        "⚡ 1M: setup + entry\n"
        "📡 Tick engine: live momentum/volatility\n"
        "💧 Liquidity + displacement + pullback\n"
        f"⭐ Quality threshold: <b>{QUALITY_THRESHOLD}/100</b>\n"
        "🧠 Feedback learning: ACTIVE\n"
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
