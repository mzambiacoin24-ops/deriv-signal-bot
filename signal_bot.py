import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from dotenv import load_dotenv

from indicators import rsi, sma
from market_movement import MovementEngine
from memory import SymbolMemory
from public_client import PublicMarketClient
from smc import SMCAnalyzer
from telegram_notifier import TelegramNotifier
from trade_tracker import TradeTracker

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
BLOCK_SAME_FEED_WHILE_ACTIVE = os.getenv("BLOCK_SAME_FEED_WHILE_ACTIVE", "1").lower() not in {"0", "false", "no"}

BROKER_MIN_POINTS = {
    ("R_10", "2s"): 720, ("R_25", "2s"): 423, ("R_50", "2s"): 1350,
    ("R_75", "2s"): 10770, ("R_100", "2s"): 138, ("1HZ10V", "1s"): 106,
    ("1HZ25V", "1s"): 10215, ("1HZ50V", "1s"): 6996, ("1HZ75V", "1s"): 432,
    ("1HZ100V", "1s"): 72,
}
BROKER_POINT_SIZE = 0.01

POINT_VALUES = {}
for item in os.getenv("POINT_VALUES", "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1").split(","):
    if "=" in item:
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
        "close": float(candle["close"]), "granularity": int(candle.get("granularity", 60)),
    }


def candle_range(c):
    return max(0.0, float(c["high"]) - float(c["low"]))


def market_regime(structure):
    candles = list(structure.candles)
    if len(candles) < 20:
        return "UNKNOWN"
    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c) > 0]
    if not ranges:
        return "UNKNOWN"
    avg = sum(ranges) / len(ranges)
    recent = candles[-6:]
    move = recent[-1]["close"] - recent[0]["open"]
    if abs(move) >= avg * 2.0:
        return "EXPANSION_UP" if move > 0 else "EXPANSION_DOWN"
    if structure.trend == "up" and structure.structure_strength in ("MODERATE", "STRONG"):
        return "TREND_UP"
    if structure.trend == "down" and structure.structure_strength in ("MODERATE", "STRONG"):
        return "TREND_DOWN"
    return "RANGE" if structure.structure_strength == "NEUTRAL" else "TRANSITION"


def volatility_location(direction, price, structure):
    """Volatility-specific location: recent/major high-low reaction zones.

    This deliberately avoids forex-style premium/discount rules.
    """
    levels = structure.get_levels()
    highs = [float(x) for x in levels.get("liquidity_highs", [])]
    lows = [float(x) for x in levels.get("liquidity_lows", [])]
    recent = [candle_range(c) for c in list(structure.candles)[-20:] if candle_range(c) > 0]
    tolerance = max(recent) * 0.8 if recent else 0.0
    if direction == "up":
        if lows and min(abs(price - x) for x in lows) <= tolerance:
            return "LOW_LIQUIDITY_ZONE"
        return "AWAY_FROM_LOW_ZONE"
    if highs and min(abs(price - x) for x in highs) <= tolerance:
        return "HIGH_LIQUIDITY_ZONE"
    return "AWAY_FROM_HIGH_ZONE"


def calculate_levels(direction, entry, structure, symbol, feed_label):
    candles = list(structure.candles)
    if len(candles) < 10:
        return None, None
    ranges = [candle_range(c) for c in candles[-20:] if candle_range(c) > 0]
    if not ranges:
        return None, None
    avg = sum(ranges) / len(ranges)
    minimum = BROKER_MIN_POINTS.get((symbol, feed_label), 0) * BROKER_POINT_SIZE * 1.05
    levels = structure.get_levels()
    highs = sorted({float(x) for x in levels.get("liquidity_highs", []) if float(x) > entry})
    lows = sorted({float(x) for x in levels.get("liquidity_lows", []) if float(x) < entry}, reverse=True)

    if direction == "up":
        supports = sorted({float(x) for x in levels.get("liquidity_lows", []) if float(x) < entry}, reverse=True)
        if not supports:
            return None, None
        sl = min(supports[0] - avg * 0.20, entry - avg * 0.55, entry - minimum)
        risk = entry - sl
        if risk <= 0:
            return None, None
        min_reward = max(minimum, risk * MIN_RR_RATIO)
        target = next((x for x in highs if x - entry >= min_reward), entry + min_reward)
        return sl, target

    resistances = sorted({float(x) for x in levels.get("liquidity_highs", []) if float(x) > entry})
    if not resistances:
        return None, None
    sl = max(resistances[0] + avg * 0.20, entry + avg * 0.55, entry + minimum)
    risk = sl - entry
    if risk <= 0:
        return None, None
    min_reward = max(minimum, risk * MIN_RR_RATIO)
    target = next((x for x in lows if entry - x >= min_reward), entry - min_reward)
    return sl, target


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
    def __init__(self):
        self.path = LEARNING_FILE
        self.min_samples = max(1, LEARNING_MIN_SAMPLES)
        self.min_win_rate = LEARNING_MIN_WIN_RATE
        self.lookback = max(20, LEARNING_LOOKBACK)
        self._lock = asyncio.Lock()
        self.data = {"version": 4, "updated_at": None, "trades": []}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("trades"), list):
                    self.data["trades"] = data["trades"]
        except Exception as exc:
            log.warning("Learning data error: %s", exc)

    def _save(self):
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _norm(v):
        return "none" if v is None else str(v).strip().lower()

    def feature_key(self, f):
        return "|".join(self._norm(f.get(k)) for k in (
            "symbol", "feed", "direction", "regime", "location", "sweep", "setup",
            "d1", "h4", "m30", "m15", "m5", "movement_direction", "volatility",
            "movement_state", "narrative_stage",
        ))

    async def evaluate(self, features):
        async with self._lock:
            key = self.feature_key(features)
            matches = [x for x in self.data["trades"] if x.get("feature_key") == key and x.get("result") in ("tp", "sl")][-self.lookback:]
            wins = sum(x.get("result") == "tp" for x in matches)
            total = len(matches)
            rate = wins / total if total else None
            decision = "LEARN" if total < self.min_samples else "ALLOW" if rate >= self.min_win_rate else "WAIT"
            return {"decision": decision, "feature_key": key, "samples": total, "wins": wins, "losses": total - wins, "win_rate": rate}

    async def register_signal(self, features):
        async with self._lock:
            key = self.feature_key(features)
            self.data["trades"].append({"created_at": datetime.now(timezone.utc).isoformat(), "feature_key": key, **{k: features.get(k) for k in features}, "result": None})
            self.data["trades"] = self.data["trades"][-5000:]
            self._save()
            return key

    async def register_result(self, key, result, exit_price=None, exit_epoch=None):
        result = str(result).lower()
        if result not in ("tp", "sl"):
            return
        async with self._lock:
            for item in reversed(self.data["trades"]):
                if item.get("feature_key") == key and item.get("result") is None:
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
        self.d1 = SMCAnalyzer(symbol, 2, 300)
        self.h4 = SMCAnalyzer(symbol, 2, 300)
        self.m30 = SMCAnalyzer(symbol, 2, 300)
        self.m15 = SMCAnalyzer(symbol, 2, 300)
        self.m5 = SMCAnalyzer(symbol, 2, 300)
        self.m1 = SMCAnalyzer(symbol, 2, 300)
        self.b1d = TimeframeBuilder(86400)
        self.b4h = TimeframeBuilder(14400)
        self.b30 = TimeframeBuilder(1800)
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
                "d1": await client.get_candles(self.symbol, 86400, CANDLE_COUNT),
                "h4": await client.get_candles(self.symbol, 14400, CANDLE_COUNT),
                "m30": await client.get_candles(self.symbol, 1800, CANDLE_COUNT),
                "m15": await client.get_candles(self.symbol, 900, CANDLE_COUNT),
                "m5": await client.get_candles(self.symbol, 300, CANDLE_COUNT),
                "m1": await client.get_candles(self.symbol, 60, CANDLE_COUNT),
            }
            for key, analyzer in (("d1", self.d1), ("h4", self.h4), ("m30", self.m30), ("m15", self.m15), ("m5", self.m5)):
                for c in histories[key]:
                    analyzer.add_candle(clean_candle(c))
            for c in histories["m1"]:
                cc = clean_candle(c)
                self.ltf_closes.append(cc["close"])
                self.m1.add_candle(cc)
                self.movement.update_tick(cc["close"], cc["epoch"], cc)
            for key, builder in (("d1", self.b1d), ("h4", self.b4h), ("m30", self.b30), ("m15", self.b15), ("m5", self.b5), ("m1", self.b1)):
                if histories[key]:
                    builder.current = clean_candle(histories[key][-1])
            self.ready = True
            log.info("[%s | %s] READY | 1D=%s 4H=%s 30M=%s 15M=%s 5M=%s", self.display_name, self.feed_label, self.d1.trend, self.h4.trend, self.m30.trend, self.m15.trend, self.m5.trend)
        except Exception as exc:
            log.exception("[%s | %s] History error: %s", self.display_name, self.feed_label, exc)

    async def _track_active_trade(self, price, epoch):
        if not self.tracker.is_active(self.symbol, self.feed_label):
            return
        completed = self.tracker.check_price(self.symbol, self.feed_label, price)
        if completed is None:
            return
        result = completed["result"].lower()
        event_id, learning_key = self.active_event_id, self.active_learning_key
        self.active_event_id = None
        self.active_learning_key = None
        if event_id:
            self.memory.record_result(self.symbol, event_id, result, completed["exit"], epoch)
        if learning_key:
            await self.learner.register_result(learning_key, result, completed["exit"], epoch)
        try:
            await self.telegram.send(
                f"{'✅' if result == 'tp' else '🛑'} <b>TRADE IMEKAMILIKA</b>\n\n"
                f"📡 Feed: <b>{self.feed_label}</b>\n📌 Deriv: <b>{self.symbol}</b>\n"
                f"📊 Result: <b>{result.upper()}</b>\n💰 Entry: <b>{completed['entry']:.4f}</b>\n"
                f"🏁 Exit: <b>{completed['exit']:.4f}</b>\n🎯 TP: <b>{completed['tp']:.4f}</b>\n"
                f"🛑 SL: <b>{completed['sl']:.4f}</b>\n⏱️ Duration: <b>{completed['duration_seconds']:.0f}s</b>\n\n"
                "🧠 Result imeingia kwenye feedback engine."
            )
        except Exception as exc:
            log.exception("Result Telegram error: %s", exc)

    async def on_candle(self, symbol, candle):
        if symbol != self.symbol or not self.ready or int(candle.get("granularity", 60)) != 60:
            return
        c = clean_candle(candle)
        tick_epoch = int(candle.get("tick_epoch", c["epoch"]))
        movement = self.movement.update_tick(c["close"], tick_epoch, c)
        await self._track_active_trade(c["close"], tick_epoch)
        completed_m1 = self.b1.update(c)
        if completed_m1 is None:
            return
        self.ltf_closes.append(completed_m1["close"])
        setup = self.m1.add_candle(completed_m1)
        for builder, analyzer in ((self.b5, self.m5), (self.b15, self.m15), (self.b30, self.m30), (self.b4h, self.h4), (self.b1d, self.d1)):
            completed = builder.update(completed_m1)
            if completed:
                analyzer.add_candle(completed)
        if setup:
            await self.evaluate_signal(setup, completed_m1, movement)

    async def evaluate_signal(self, setup, candle, movement):
        now = time.time()
        if BLOCK_SAME_FEED_WHILE_ACTIVE and self.tracker.is_active(self.symbol, self.feed_label):
            return
        if now - self.last_signal_time < MIN_SECONDS_BETWEEN_SIGNALS:
            return

        direction = setup.get("direction")
        if direction not in ("up", "down") or setup.get("event") != "VOLATILITY_NARRATIVE_CONFIRMED":
            return

        higher = (self.d1, self.h4, self.m30)
        opposite = "down" if direction == "up" else "up"
        strong_conflicts = sum(a.trend == opposite and a.structure_strength == "STRONG" for a in higher)
        if strong_conflicts >= 2:
            return

        price = float(candle["close"])
        regime = market_regime(self.m5)
        location = volatility_location(direction, price, self.m1)
        sweep = setup.get("sweep")
        sweep_pass = (direction == "up" and sweep == "low") or (direction == "down" and sweep == "high")
        if not sweep_pass:
            return

        movement_direction = movement.get("direction")
        rejection = movement.get("rejection", "NONE")
        volatility = movement.get("volatility", "UNKNOWN")
        movement_aligned = movement_direction == direction or (direction == "up" and rejection == "LOW_REJECTION") or (direction == "down" and rejection == "HIGH_REJECTION")
        if volatility == "LOW" or not movement_aligned:
            return

        sl, tp = calculate_levels(direction, price, self.m1, self.symbol, self.feed_label)
        if sl is None or tp is None:
            return
        risk = abs(price - sl)
        reward = abs(tp - price)
        if risk <= 0 or reward / risk < MIN_RR_RATIO:
            return
        rr = reward / risk

        quality = 40
        reasons = ["NARRATIVE_COMPLETE"]
        if self.m15.trend == direction:
            quality += 10; reasons.append("M15")
        if self.m5.trend == direction:
            quality += 10; reasons.append("M5")
        if movement_aligned:
            quality += 12; reasons.append("LIVE_MOMENTUM")
        if volatility == "EXPANDING":
            quality += 10; reasons.append("VOL_EXPANSION")
        elif volatility == "NORMAL":
            quality += 5
        if movement.get("candle_body_ratio", 0) >= 0.55:
            quality += 5
        if self.d1.trend == direction:
            quality += 5
        if self.h4.trend == direction:
            quality += 5
        if self.m30.trend == direction:
            quality += 3
        if rr >= 2:
            quality += 4
        quality = max(0, min(100, quality))
        if quality < QUALITY_THRESHOLD:
            log.info("[%s | %s] QUALITY WAIT %s/100 narrative=%s", self.display_name, self.feed_label, quality, setup.get("timing"))
            return

        features = {
            "symbol": self.symbol, "feed": self.feed_label, "direction": direction,
            "regime": regime, "location": location, "sweep": sweep,
            "setup": setup.get("reason"), "d1": self.d1.trend, "h4": self.h4.trend,
            "m30": self.m30.trend, "m15": self.m15.trend, "m5": self.m5.trend,
            "movement_direction": movement_direction, "volatility": volatility,
            "movement_state": "ALIGNED", "narrative_stage": setup.get("timing"),
            "movement_score": movement.get("score", 0), "movement_pressure": movement.get("pressure"),
            "rr": rr,
        }
        learning = await self.learner.evaluate(features)
        if learning["decision"] == "WAIT":
            log.info("[%s | %s] LEARNING WAIT samples=%s winrate=%s", self.display_name, self.feed_label, learning["samples"], learning["win_rate"])
            return

        confidence = "VERY HIGH" if quality >= 88 else "HIGH" if quality >= 80 else "GOOD"
        lot = None
        if self.point_value and self.point_value > 0:
            risk_money = ACCOUNT_BALANCE * RISK_PERCENT_PER_TRADE / 100
            lot = max(round(risk_money / (risk * self.point_value), 2), 0.01)

        action = "NUNUA (BUY)" if direction == "up" else "UZA (SELL)"
        icon = "📈" if direction == "up" else "📉"
        fvg = setup.get("fvg") or {}
        target_liquidity = setup.get("target_liquidity")
        tf = lambda a: f"{(a.trend or 'N/A').upper()} ({a.structure_strength})"
        message = (
            f"{icon} <b>ADVISORY SIGNAL: {action}</b>\n\n"
            f"📡 Feed: <b>{self.feed_label}</b>\n📌 Deriv: <b>{self.symbol}</b>\n📍 MT5: <b>{self.display_name}</b>\n"
            f"⭐ Quality: <b>{quality}/100 — {confidence}</b>\n\n"
            f"💰 Entry: <b>{price:.4f}</b>\n🎯 TP: <b>{tp:.4f}</b>\n🛑 SL: <b>{sl:.4f}</b>\n⚖️ R:R: <b>1:{rr:.2f}</b>\n📊 Lot: <b>{lot or 'N/A'}</b>\n\n"
            f"🌍 1D: <b>{tf(self.d1)}</b>\n🕓 4H: <b>{tf(self.h4)}</b>\n🕧 30M: <b>{tf(self.m30)}</b>\n"
            f"🧠 15M: <b>{tf(self.m15)}</b>\n🔄 5M: <b>{tf(self.m5)}</b>\n⚡ 1M: <b>{direction.upper()}</b>\n\n"
            f"🌐 Regime: <b>{regime}</b>\n📍 Volatility location: <b>{location}</b>\n"
            f"💧 Liquidity: <b>{'SSL TAKEN' if sweep == 'low' else 'BSL TAKEN'}</b> @ <b>{setup.get('liquidity_level') or 0:.4f}</b>\n"
            f"🚀 Narrative: <b>{setup.get('timing')}</b>\n"
            f"🧱 MSS level: <b>{setup.get('mss_level') or 0:.4f}</b>\n"
            f"🟩 FVG: <b>{fvg.get('low', 0):.4f} - {fvg.get('high', 0):.4f}</b>\n"
            f"🎯 Target liquidity: <b>{target_liquidity or 0:.4f}</b>\n"
            f"⚡ Live movement: <b>{movement_direction or 'N/A'}</b> | Volatility: <b>{volatility}</b> ({movement.get('range_ratio', 0):.2f}x)\n"
            f"🧠 Feedback: <b>{learning['decision']}</b> ({learning['samples']} samples)\n"
            f"🧩 Reasons: <b>{', '.join(reasons)}</b>\n\n"
            "⚠️ <i>Advisory only. Bot hai-trade.</i>"
        )

        try:
            await self.telegram.send(message)
            signal_data = {**features, "confidence": confidence, "quality": quality, "entry": price, "tp": tp, "sl": sl, "rr": rr, "entry_epoch": int(candle.get("epoch", time.time()))}
            event_id = self.memory.record_signal(self.symbol, self.feed_label, self.display_name, signal_data)
            if not self.tracker.register(self.symbol, self.feed_label, direction, price, tp, sl, self.display_name):
                log.warning("[%s | %s] Tracker rejected after signal send.", self.display_name, self.feed_label)
                return
            self.active_event_id = event_id
            self.active_learning_key = await self.learner.register_signal(signal_data)
            self.last_signal_time = now
            log.info("[%s | %s] SIGNAL %s quality=%s entry=%.4f sl=%.4f tp=%.4f rr=%.2f", self.display_name, self.feed_label, action, quality, price, sl, tp, rr)
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
    monitors = [PairMonitor(*item, telegram, tracker, memory, learner) for item in SYMBOLS]
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
            log.info("[%s | %s] Live M1 stream started.", monitor.display_name, monitor.feed_label)
            await asyncio.sleep(0.5)
        except Exception as exc:
            log.exception("[%s | %s] Stream start error: %s", monitor.display_name, monitor.feed_label, exc)

    await telegram.send(
        "🤖 <b>Volatility Advisory Engine v9</b>\n\n"
        "🌍 1D + 4H + 30M: long-movement context\n"
        "🧠 15M + 5M: directional context\n"
        "⚡ 1M: liquidity-to-entry narrative\n"
        "💧 Liquidity → displacement → MSS → FVG → retracement → confirmation\n"
        "📍 Previous highs/lows are treated as reaction/liquidity zones\n"
        f"⭐ Quality threshold: <b>{QUALITY_THRESHOLD}/100</b>\n"
        "🧠 Feedback learning: ACTIVE\n"
        "🔒 Same symbol/feed conflict protection: ACTIVE\n"
        "🔓 Global signal lock: OFF\n\n"
        "⚠️ Advisory only — bot hai-trade."
    )
    try:
        while True:
            await asyncio.sleep(60)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
