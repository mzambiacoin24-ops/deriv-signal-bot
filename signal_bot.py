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

LEARNING_FILE = os.getenv(
    "LEARNING_FILE",
    "learning_data.json",
).strip()

LEARNING_MIN_SAMPLES = int(
    os.getenv("LEARNING_MIN_SAMPLES", "8")
)

LEARNING_LOOKBACK = int(
    os.getenv("LEARNING_LOOKBACK", "100")
)

# Learning is advisory, not a hard lock.
# It can increase/decrease setup confidence once enough
# real results exist, but it cannot invent a direction.
LEARNING_MIN_WIN_RATE = float(
    os.getenv("LEARNING_MIN_WIN_RATE", "0.52")
)

BLOCK_SAME_FEED_WHILE_ACTIVE = (
    os.getenv(
        "BLOCK_SAME_FEED_WHILE_ACTIVE",
        "1",
    ).lower()
    not in {"0", "false", "no"}
)

BROKER_MIN_POINTS = {
    ("R_100", "2s"): 138,
    ("1HZ100V", "1s"): 72,
    ("R_50", "2s"): 1350,
}

BROKER_POINT_SIZE = 0.01

POINT_VALUES = {}

for item in os.getenv(
    "POINT_VALUES",
    "R_50=1,R_100=1",
).split(","):
    if "=" not in item:
        continue

    symbol, value = item.split("=", 1)

    try:
        POINT_VALUES[symbol.strip()] = float(
            value.strip()
        )
    except ValueError:
        pass


# =============================================================
# VOLATILITY UNIVERSE
# =============================================================

SYMBOLS = [
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
    (
        "R_50",
        "Volatility 50 Index",
        "2s",
        "R_50",
    ),
]


def clean_candle(candle):
    return {
        "epoch": int(candle["epoch"]),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "granularity": int(
            candle.get("granularity", 60)
        ),
    }


def candle_range(candle):
    return max(
        0.0,
        float(candle["high"])
        - float(candle["low"]),
    )


def body_ratio(candle):
    rng = candle_range(candle)

    if rng <= 0:
        return 0.0

    return abs(
        float(candle["close"])
        - float(candle["open"])
    ) / rng


def market_regime(structure):
    candles = list(structure.candles)

    if len(candles) < 20:
        return "UNKNOWN"

    ranges = [
        candle_range(c)
        for c in candles[-20:]
        if candle_range(c) > 0
    ]

    if not ranges:
        return "UNKNOWN"

    avg = sum(ranges) / len(ranges)

    recent = candles[-6:]

    move = (
        float(recent[-1]["close"])
        - float(recent[0]["open"])
    )

    if abs(move) >= avg * 2.0:
        return (
            "EXPANSION_UP"
            if move > 0
            else "EXPANSION_DOWN"
        )

    if (
        structure.trend == "up"
        and structure.structure_strength
        in ("MODERATE", "STRONG")
    ):
        return "TREND_UP"

    if (
        structure.trend == "down"
        and structure.structure_strength
        in ("MODERATE", "STRONG")
    ):
        return "TREND_DOWN"

    if structure.structure_strength == "NEUTRAL":
        return "RANGE"

    return "TRANSITION"


def _levels(structure):
    """
    Works with both the old and newer SMC implementations.
    """
    try:
        levels = structure.get_levels()
    except Exception:
        levels = {}

    if not isinstance(levels, dict):
        levels = {}

    highs = (
        levels.get("liquidity_highs")
        or levels.get("swing_highs")
        or []
    )

    lows = (
        levels.get("liquidity_lows")
        or levels.get("swing_lows")
        or []
    )

    return (
        sorted(
            {
                float(x)
                for x in highs
            }
        ),
        sorted(
            {
                float(x)
                for x in lows
            }
        ),
    )


def nearest_reaction_zone(
    direction,
    price,
    structure,
):
    """
    Volatility-specific reaction model.

    We do NOT use forex/crypto premium-discount logic.

    Previous highs/lows are treated as areas where the market
    may react. They are not automatic reversal signals.
    Confirmation is still required on 5M/1M.
    """
    highs, lows = _levels(structure)

    candles = list(structure.candles)

    ranges = [
        candle_range(c)
        for c in candles[-20:]
        if candle_range(c) > 0
    ]

    if not ranges:
        return None

    avg_range = sum(ranges) / len(ranges)

    # Wider than a tiny tick tolerance, but still relative
    # to the actual recent volatility.
    tolerance = avg_range * 0.35

    if direction == "up":
        candidates = [
            x
            for x in lows
            if x <= price
        ]

        if not candidates:
            return None

        level = max(candidates)

    else:
        candidates = [
            x
            for x in highs
            if x >= price
        ]

        if not candidates:
            return None

        level = min(candidates)

    distance = abs(price - level)

    if distance <= tolerance:
        return {
            "type": (
                "PREVIOUS_LOW"
                if direction == "up"
                else "PREVIOUS_HIGH"
            ),
            "level": level,
            "distance": distance,
            "tolerance": tolerance,
        }

    return None


def recent_opposite_level(
    direction,
    price,
    structure,
):
    """
    Target for TP.

    BUY -> previous high above entry.
    SELL -> previous low below entry.
    """
    highs, lows = _levels(structure)

    if direction == "up":
        candidates = [
            x
            for x in highs
            if x > price
        ]

        return min(candidates) if candidates else None

    candidates = [
        x
        for x in lows
        if x < price
    ]

    return max(candidates) if candidates else None


def calculate_levels(
    direction,
    entry,
    structure,
    symbol,
    feed_label,
):
    candles = list(structure.candles)

    if len(candles) < 10:
        return None, None

    ranges = [
        candle_range(c)
        for c in candles[-20:]
        if candle_range(c) > 0
    ]

    if not ranges:
        return None, None

    avg = sum(ranges) / len(ranges)

    minimum = (
        BROKER_MIN_POINTS.get(
            (symbol, feed_label),
            0,
        )
        * BROKER_POINT_SIZE
        * 1.05
    )

    highs, lows = _levels(structure)

    if direction == "up":
        supports = [
            x
            for x in lows
            if x < entry
        ]

        if supports:
            support = max(supports)
            sl = min(
                support - avg * 0.20,
                entry - avg * 0.55,
                entry - minimum,
            )
        else:
            sl = min(
                entry - avg * 0.80,
                entry - minimum,
            )

        risk = entry - sl

        if risk <= 0:
            return None, None

        minimum_reward = max(
            minimum,
            risk * MIN_RR_RATIO,
        )

        target = recent_opposite_level(
            direction,
            entry,
            structure,
        )

        if (
            target is None
            or target - entry < minimum_reward
        ):
            target = (
                entry
                + minimum_reward
            )

        return sl, target

    resistances = [
        x
        for x in highs
        if x > entry
    ]

    if resistances:
        resistance = min(resistances)

        sl = max(
            resistance + avg * 0.20,
            entry + avg * 0.55,
            entry + minimum,
        )
    else:
        sl = max(
            entry + avg * 0.80,
            entry + minimum,
        )

    risk = sl - entry

    if risk <= 0:
        return None, None

    minimum_reward = max(
        minimum,
        risk * MIN_RR_RATIO,
    )

    target = recent_opposite_level(
        direction,
        entry,
        structure,
    )

    if (
        target is None
        or entry - target < minimum_reward
    ):
        target = (
            entry
            - minimum_reward
        )

    return sl, target


class TimeframeBuilder:
    def __init__(self, seconds):
        self.seconds = int(seconds)
        self.current = None

    def update(self, candle):
        epoch = int(candle["epoch"])

        bucket = (
            epoch
            - epoch % self.seconds
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


class AdaptiveLearningEngine:
    """
    Feedback engine.

    Muhimu:
    - Inakumbuka matokeo.
    - Inapima performance kwa symbol + feed + direction +
      regime + location + setup.
    - Matokeo yanaathiri confidence ya signal inayofuata.
    - Haitoi BUY/SELL yenyewe bila market structure.
    - Haitumii learning kama global lock.
    """

    def __init__(self):
        self.path = LEARNING_FILE

        self.min_samples = max(
            1,
            LEARNING_MIN_SAMPLES,
        )

        self.lookback = max(
            20,
            LEARNING_LOOKBACK,
        )

        self._lock = asyncio.Lock()

        self.data = {
            "version": 5,
            "updated_at": None,
            "trades": [],
        }

        self._load()

    def _load(self):
        try:
            if not os.path.exists(
                self.path
            ):
                return

            with open(
                self.path,
                "r",
                encoding="utf-8",
            ) as handle:
                data = json.load(handle)

            if (
                isinstance(data, dict)
                and isinstance(
                    data.get("trades"),
                    list,
                )
            ):
                self.data["trades"] = (
                    data["trades"]
                )

        except Exception as exc:
            log.warning(
                "Learning data error: %s",
                exc,
            )

    def _save(self):
        self.data["updated_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        tmp = (
            f"{self.path}.tmp"
        )

        with open(
            tmp,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                self.data,
                handle,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            tmp,
            self.path,
        )

    @staticmethod
    def _norm(value):
        if value is None:
            return "none"

        return str(
            value
        ).strip().lower()

    def feature_key(self, features):
        """
        Key intentionally excludes exact price/epoch so the
        engine learns recurring market behaviour instead of
        memorising individual candles.
        """
        keys = (
            "symbol",
            "feed",
            "direction",
            "regime",
            "location",
            "sweep",
            "setup",
            "d1",
            "m15",
            "m5",
            "movement_direction",
            "volatility",
            "movement_state",
            "reaction_type",
        )

        return "|".join(
            self._norm(
                features.get(key)
            )
            for key in keys
        )

    async def evaluate(
        self,
        features,
    ):
        async with self._lock:
            key = self.feature_key(
                features
            )

            completed = [
                item
                for item in self.data["trades"]
                if (
                    item.get(
                        "feature_key"
                    )
                    == key
                    and item.get(
                        "result"
                    )
                    in ("tp", "sl")
                )
            ][-self.lookback:]

            wins = sum(
                item.get("result")
                == "tp"
                for item in completed
            )

            total = len(completed)

            rate = (
                wins / total
                if total
                else None
            )

            # -----------------------------------------------------
            # Learning modifies confidence, not raw direction.
            # -----------------------------------------------------

            adjustment = 0

            if total >= self.min_samples:
                if rate >= 0.65:
                    adjustment = 12
                elif rate >= 0.58:
                    adjustment = 7
                elif rate >= 0.52:
                    adjustment = 3
                elif rate >= 0.45:
                    adjustment = -6
                else:
                    adjustment = -12

            # Decision is deliberately not a hard WAIT when there
            # is a good live setup. This prevents the bot becoming
            # silent because of a small historical sample.
            if total < self.min_samples:
                decision = "LEARNING"
            elif rate >= 0.58:
                decision = "ADAPTIVE_ALLOW"
            elif rate >= 0.45:
                decision = "NEUTRAL"
            else:
                decision = "CAUTION"

            return {
                "decision": decision,
                "feature_key": key,
                "samples": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": rate,
                "adjustment": adjustment,
            }

    async def register_signal(
        self,
        features,
    ):
        async with self._lock:
            key = self.feature_key(
                features
            )

            item = {
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "feature_key": key,
                **{
                    key: features.get(key)
                    for key in features
                },
                "result": None,
            }

            self.data["trades"].append(
                item
            )

            self.data["trades"] = (
                self.data["trades"][-5000:]
            )

            self._save()

            return key

    async def register_result(
        self,
        key,
        result,
        exit_price=None,
        exit_epoch=None,
    ):
        result = str(
            result
        ).lower()

        if result not in (
            "tp",
            "sl",
        ):
            return

        async with self._lock:
            for item in reversed(
                self.data["trades"]
            ):
                if (
                    item.get(
                        "feature_key"
                    )
                    == key
                    and item.get(
                        "result"
                    )
                    is None
                ):
                    item["result"] = (
                        result
                    )
                    item["exit"] = (
                        exit_price
                    )
                    item[
                        "exit_epoch"
                    ] = exit_epoch
                    item[
                        "completed_at"
                    ] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    break

            self._save()


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

        # =========================================================
        # ONLY:
        # 1D = daily bias
        # 15M = context / reaction zones
        # 5M = confirmation
        # 1M = entry timing
        # =========================================================

        self.d1 = SMCAnalyzer(
            symbol,
            2,
            300,
        )

        self.m15 = SMCAnalyzer(
            symbol,
            2,
            300,
        )

        self.m5 = SMCAnalyzer(
            symbol,
            2,
            300,
        )

        self.m1 = SMCAnalyzer(
            symbol,
            2,
            300,
        )

        self.b1d = TimeframeBuilder(
            86400
        )

        self.b15 = TimeframeBuilder(
            900
        )

        self.b5 = TimeframeBuilder(
            300
        )

        self.b1 = TimeframeBuilder(
            60
        )

        self.ltf_closes = deque(
            maxlen=100
        )

        self.movement = MovementEngine()

        self.last_signal_time = 0
        self.active_event_id = None
        self.active_learning_key = None

        self.ready = False

        self.point_value = (
            POINT_VALUES.get(
                point_symbol
            )
        )

    async def initialize(
        self,
        client,
    ):
        try:
            histories = {
                "d1": await client.get_candles(
                    self.symbol,
                    86400,
                    CANDLE_COUNT,
                ),
                "m15": await client.get_candles(
                    self.symbol,
                    900,
                    CANDLE_COUNT,
                ),
                "m5": await client.get_candles(
                    self.symbol,
                    300,
                    CANDLE_COUNT,
                ),
                "m1": await client.get_candles(
                    self.symbol,
                    60,
                    CANDLE_COUNT,
                ),
            }

            for key, analyzer in (
                (
                    "d1",
                    self.d1,
                ),
                (
                    "m15",
                    self.m15,
                ),
                (
                    "m5",
                    self.m5,
                ),
            ):
                for candle in histories[
                    key
                ]:
                    analyzer.add_candle(
                        clean_candle(
                            candle
                        )
                    )

            for candle in histories[
                "m1"
            ]:
                cc = clean_candle(
                    candle
                )

                self.ltf_closes.append(
                    cc["close"]
                )

                self.m1.add_candle(
                    cc
                )

                self.movement.update_tick(
                    cc["close"],
                    cc["epoch"],
                    cc,
                )

            for key, builder in (
                (
                    "d1",
                    self.b1d,
                ),
                (
                    "m15",
                    self.b15,
                ),
                (
                    "m5",
                    self.b5,
                ),
                (
                    "m1",
                    self.b1,
                ),
            ):
                if histories[key]:
                    builder.current = (
                        clean_candle(
                            histories[key][-1]
                        )
                    )

            self.ready = True

            log.info(
                "[%s | %s] READY | "
                "1D=%s | 15M=%s | 5M=%s | 1M=%s",
                self.display_name,
                self.feed_label,
                self.d1.trend,
                self.m15.trend,
                self.m5.trend,
                self.m1.trend,
            )

        except Exception as exc:
            log.exception(
                "[%s | %s] History error: %s",
                self.display_name,
                self.feed_label,
                exc,
            )

    async def _track_active_trade(
        self,
        price,
        epoch,
    ):
        if not self.tracker.is_active(
            self.symbol,
            self.feed_label,
        ):
            return

        completed = (
            self.tracker.check_price(
                self.symbol,
                self.feed_label,
                price,
            )
        )

        if completed is None:
            return

        result = str(
            completed["result"]
        ).lower()

        event_id = (
            self.active_event_id
        )

        learning_key = (
            self.active_learning_key
        )

        self.active_event_id = None
        self.active_learning_key = None

        if event_id:
            self.memory.record_result(
                self.symbol,
                event_id,
                result,
                completed["exit"],
                epoch,
            )

        if learning_key:
            await self.learner.register_result(
                learning_key,
                result,
                completed["exit"],
                epoch,
            )

        try:
            await self.telegram.send(
                f"{'✅' if result == 'tp' else '🛑'} "
                "<b>TRADE IMEKAMILIKA</b>\n\n"
                f"📡 Feed: <b>{self.feed_label}</b>\n"
                f"📌 Deriv: <b>{self.symbol}</b>\n"
                f"📊 Result: <b>{result.upper()}</b>\n"
                f"💰 Entry: <b>{completed['entry']:.4f}</b>\n"
                f"🏁 Exit: <b>{completed['exit']:.4f}</b>\n"
                f"🎯 TP: <b>{completed['tp']:.4f}</b>\n"
                f"🛑 SL: <b>{completed['sl']:.4f}</b>\n"
                f"⏱️ Duration: "
                f"<b>{completed['duration_seconds']:.0f}s</b>\n\n"
                "🧠 Result imeingia kwenye "
                "feedback engine."
            )

        except Exception as exc:
            log.exception(
                "Result Telegram error: %s",
                exc,
            )

    async def on_candle(
        self,
        symbol,
        candle,
    ):
        if (
            symbol != self.symbol
            or not self.ready
            or int(
                candle.get(
                    "granularity",
                    60,
                )
            )
            != 60
        ):
            return

        c = clean_candle(
            candle
        )

        tick_epoch = int(
            candle.get(
                "tick_epoch",
                c["epoch"],
            )
        )

        movement = (
            self.movement.update_tick(
                c["close"],
                tick_epoch,
                c,
            )
        )

        await self._track_active_trade(
            c["close"],
            tick_epoch,
        )

        completed_m1 = (
            self.b1.update(c)
        )

        if completed_m1 is None:
            return

        self.ltf_closes.append(
            completed_m1["close"]
        )

        setup = self.m1.add_candle(
            completed_m1
        )

        # Higher timeframe updates.
        for builder, analyzer in (
            (
                self.b5,
                self.m5,
            ),
            (
                self.b15,
                self.m15,
            ),
            (
                self.b1d,
                self.d1,
            ),
        ):
            completed = builder.update(
                completed_m1
            )

            if completed:
                analyzer.add_candle(
                    completed
                )

        if setup:
            await self.evaluate_signal(
                setup,
                completed_m1,
                movement,
            )

    def _m1_confirmation(
        self,
        direction,
    ):
        """
        Final 1M timing.

        We want a reaction followed by continuation,
        not a huge chasing candle.
        """
        candles = list(
            self.m1.candles
        )

        if len(candles) < 5:
            return False, "NOT_ENOUGH_1M"

        c1 = candles[-3]
        c2 = candles[-2]
        c3 = candles[-1]

        ranges = [
            candle_range(c)
            for c in candles[-12:-1]
            if candle_range(c) > 0
        ]

        if not ranges:
            return False, "NO_RANGE"

        avg = sum(ranges) / len(
            ranges
        )

        latest = candle_range(c3)

        # Avoid chasing abnormal expansion.
        if latest > avg * 2.20:
            return False, "CHASE_RISK"

        if direction == "up":
            pullback = (
                float(c2["low"])
                <= float(c1["low"])
                or float(c2["close"])
                < float(c1["close"])
            )

            confirmation = (
                float(c3["close"])
                > float(c3["open"])
                and float(c3["close"])
                > float(c2["close"])
            )

            if pullback and confirmation:
                return True, "PULLBACK_RECLAIM"

        else:
            pullback = (
                float(c2["high"])
                >= float(c1["high"])
                or float(c2["close"])
                > float(c1["close"])
            )

            confirmation = (
                float(c3["close"])
                < float(c3["open"])
                and float(c3["close"])
                < float(c2["close"])
            )

            if pullback and confirmation:
                return True, "PULLBACK_REJECT"

        # A clean rejection candle can also be valid if the
        # previous high/low zone is confirmed.
        bullish_rejection = (
            float(c3["close"])
            > float(c3["open"])
            and (
                float(c3["close"])
                - float(c3["low"])
            )
            > (
                float(c3["high"])
                - float(c3["close"])
            )
        )

        bearish_rejection = (
            float(c3["close"])
            < float(c3["open"])
            and (
                float(c3["high"])
                - float(c3["close"])
            )
            > (
                float(c3["close"])
                - float(c3["low"])
            )
        )

        if (
            direction == "up"
            and bullish_rejection
        ):
            return True, "LOW_ZONE_REJECTION"

        if (
            direction == "down"
            and bearish_rejection
        ):
            return True, "HIGH_ZONE_REJECTION"

        return False, "WAIT_CONFIRMATION"

    async def evaluate_signal(
        self,
        setup,
        candle,
        movement,
    ):
        now = time.time()

        if (
            BLOCK_SAME_FEED_WHILE_ACTIVE
            and self.tracker.is_active(
                self.symbol,
                self.feed_label,
            )
        ):
            return

        if (
            now - self.last_signal_time
            < MIN_SECONDS_BETWEEN_SIGNALS
        ):
            return

        direction = setup.get(
            "direction"
        )

        if direction not in (
            "up",
            "down",
        ):
            return

        # =========================================================
        # 1D = DAILY BIAS ONLY
        # =========================================================

        daily_bias = self.d1.trend

        if daily_bias not in (
            "up",
            "down",
        ):
            log.info(
                "[%s | %s] WAIT: no 1D bias",
                self.display_name,
                self.feed_label,
            )
            return

        # We do not allow an entry against the daily bias.
        if direction != daily_bias:
            return

        # =========================================================
        # 15M = CONTEXT
        # =========================================================

        m15_direction = self.m15.trend

        if m15_direction not in (
            "up",
            "down",
        ):
            return

        # 15M does not have to be perfect/strong. It must not
        # strongly contradict the daily direction.
        if (
            m15_direction
            != direction
            and self.m15.structure_strength
            == "STRONG"
        ):
            return

        # =========================================================
        # 5M = SETUP CONFIRMATION
        # =========================================================

        m5_direction = self.m5.trend

        if (
            m5_direction
            not in (
                "up",
                "down",
            )
        ):
            return

        if (
            m5_direction
            != direction
            and self.m5.structure_strength
            == "STRONG"
        ):
            return

        price = float(
            candle["close"]
        )

        regime = market_regime(
            self.m5
        )

        # =========================================================
        # PREVIOUS HIGH/LOW REACTION ZONE
        # =========================================================

        reaction = (
            nearest_reaction_zone(
                direction,
                price,
                self.m15,
            )
            or nearest_reaction_zone(
                direction,
                price,
                self.m5,
            )
        )

        # A reaction zone is preferred, but we also allow a valid
        # 5M/1M structure setup so the bot does not become silent.
        zone_bonus = 0

        if reaction:
            zone_bonus = 18

        # =========================================================
        # MOVEMENT
        # =========================================================

        movement_direction = (
            movement.get("direction")
        )

        rejection = movement.get(
            "rejection",
            "NONE",
        )

        volatility = movement.get(
            "volatility",
            "UNKNOWN",
        )

        movement_aligned = (
            movement_direction
            == direction
            or (
                direction == "up"
                and rejection
                == "LOW_REJECTION"
            )
            or (
                direction == "down"
                and rejection
                == "HIGH_REJECTION"
            )
        )

        if not movement_aligned:
            return

        if volatility == "LOW":
            return

        # =========================================================
        # SWEEP = CONFIRMATION, NOT THE WHOLE STRATEGY
        # =========================================================

        sweep = setup.get(
            "sweep"
        )

        sweep_pass = (
            direction == "up"
            and sweep == "low"
        ) or (
            direction == "down"
            and sweep == "high"
        )

        # If SMC has a sweep, reward it.
        # Do not make FVG or sweep the only doorway.
        sweep_bonus = (
            14
            if sweep_pass
            else 0
        )

        # =========================================================
        # 1M ENTRY TIMING
        # =========================================================

        timing_ok, timing_reason = (
            self._m1_confirmation(
                direction
            )
        )

        if not timing_ok:
            return

        # =========================================================
        # RSI / SMA = SUPPORTING INFORMATION ONLY
        # =========================================================

        rsi_value = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        sma_value = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        indicator_bonus = 0

        if rsi_value is not None:
            if direction == "up":
                if 35 <= rsi_value < 70:
                    indicator_bonus += 5
            else:
                if 30 < rsi_value <= 65:
                    indicator_bonus += 5

        if sma_value is not None:
            if direction == "up":
                if price >= sma_value:
                    indicator_bonus += 4
            else:
                if price <= sma_value:
                    indicator_bonus += 4

        # =========================================================
        # LEVELS
        # =========================================================

        sl, tp = calculate_levels(
            direction,
            price,
            self.m5,
            self.symbol,
            self.feed_label,
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

        rr = reward / risk

        if rr < MIN_RR_RATIO:
            return

        # =========================================================
        # BASE QUALITY
        # =========================================================

        quality = 42

        reasons = []

        if self.d1.trend == direction:
            quality += 8
            reasons.append(
                "1D_BIAS"
            )

        if (
            self.m15.trend
            == direction
        ):
            quality += 8
            reasons.append(
                "15M_ALIGNMENT"
            )

        if (
            self.m5.trend
            == direction
        ):
            quality += 8
            reasons.append(
                "5M_ALIGNMENT"
            )

        quality += zone_bonus

        if reaction:
            reasons.append(
                reaction["type"]
            )

        quality += sweep_bonus

        if sweep_pass:
            reasons.append(
                "LIQUIDITY_SWEEP"
            )

        if movement_aligned:
            quality += 8
            reasons.append(
                "LIVE_MOVEMENT"
            )

        if volatility == "EXPANDING":
            quality += 7
            reasons.append(
                "EXPANSION"
            )

        elif volatility == "NORMAL":
            quality += 3

        quality += indicator_bonus

        if indicator_bonus:
            reasons.append(
                "INDICATOR_SUPPORT"
            )

        if rr >= 2:
            quality += 5
            reasons.append(
                "RR_2_PLUS"
            )

        if (
            timing_reason
            in (
                "LOW_ZONE_REJECTION",
                "HIGH_ZONE_REJECTION",
            )
        ):
            quality += 5
            reasons.append(
                "1M_REJECTION"
            )
        else:
            reasons.append(
                "1M_CONFIRMATION"
            )

        quality = max(
            0,
            min(
                100,
                quality,
            ),
        )

        # =========================================================
        # LEARNING
        # =========================================================

        features = {
            "symbol": self.symbol,
            "feed": self.feed_label,
            "direction": direction,
            "regime": regime,
            "location": (
                reaction["type"]
                if reaction
                else "NO_NEAR_ZONE"
            ),
            "sweep": sweep,
            "setup": setup.get(
                "reason",
                timing_reason,
            ),
            "d1": self.d1.trend,
            "m15": self.m15.trend,
            "m5": self.m5.trend,
            "movement_direction": (
                movement_direction
            ),
            "volatility": volatility,
            "movement_state": (
                "ALIGNED"
            ),
            "reaction_type": (
                reaction["type"]
                if reaction
                else "NONE"
            ),
            "movement_score": movement.get(
                "score",
                0,
            ),
            "movement_pressure": movement.get(
                "pressure"
            ),
            "rr": rr,
        }

        # =========================================================
        # PERSISTENT SYMBOL/FEED MEMORY
        # =========================================================
        memory_stats = self.memory.get_pattern_stats(
            self.symbol,
            self.feed_label,
            direction,
            features.get("setup"),
            sweep,
            regime,
            volatility,
            features.get("reaction_type", "NONE"),
        )

        memory_samples = int(
            memory_stats.get("samples", 0)
        )
        memory_win_rate = memory_stats.get("tp_rate")

        learning = (
            await self.learner.evaluate(
                features
            )
        )

        adaptive_adjustment = int(
            learning.get(
                "adjustment",
                0,
            )
        )

        # Persistent memory changes confidence, but never creates
        # a BUY/SELL direction by itself.
        if (
            memory_samples >= LEARNING_MIN_SAMPLES
            and memory_win_rate is not None
        ):
            if memory_win_rate >= 65:
                adaptive_adjustment += 10
            elif memory_win_rate >= 58:
                adaptive_adjustment += 6
            elif memory_win_rate >= 52:
                adaptive_adjustment += 2
            elif memory_win_rate >= 45:
                adaptive_adjustment -= 4
            else:
                adaptive_adjustment -= 8

        adaptive_adjustment = max(
            -15,
            min(
                15,
                adaptive_adjustment,
            ),
        )

        adaptive_quality = max(
            0,
            min(
                100,
                quality
                + adaptive_adjustment,
            ),
        )

        # Do not require a fixed 72 threshold.
        # Strong structural setups can pass around the mid-60s,
        # while weak learned combinations must earn their way up.
        minimum_quality = 65

        if (
            adaptive_quality
            < minimum_quality
        ):
            log.info(
                "[%s | %s] QUALITY WAIT "
                "base=%s adaptive=%s "
                "learning=%s samples=%s",
                self.display_name,
                self.feed_label,
                quality,
                adaptive_quality,
                learning["decision"],
                learning["samples"],
            )
            return

        # A badly performing learned setup can still be allowed
        # when the live structure is unusually strong. This avoids
        # turning learning into a permanent deadlock.
        if (
            learning["decision"]
            == "CAUTION"
            and adaptive_quality < 72
            and not (
                reaction
                and sweep_pass
                and rr >= 2
                and self.m5.structure_strength
                == "STRONG"
            )
        ):
            return

        # =========================================================
        # LOT
        # =========================================================

        lot = None

        if (
            self.point_value
            and self.point_value > 0
        ):
            risk_money = (
                ACCOUNT_BALANCE
                * RISK_PERCENT_PER_TRADE
                / 100
            )

            lot = max(
                round(
                    risk_money
                    / (
                        risk
                        * self.point_value
                    ),
                    2,
                ),
                0.01,
            )

        # =========================================================
        # MESSAGE
        # =========================================================

        action = (
            "NUNUA (BUY)"
            if direction == "up"
            else "UZA (SELL)"
        )

        icon = (
            "📈"
            if direction == "up"
            else "📉"
        )

        confidence = (
            "VERY HIGH"
            if adaptive_quality >= 88
            else "HIGH"
            if adaptive_quality >= 78
            else "GOOD"
        )

        def tf(analyzer):
            trend = (
                analyzer.trend
                or "N/A"
            )

            return (
                f"{trend.upper()} "
                f"({analyzer.structure_strength})"
            )

        if rsi_value is None:
            rsi_text = "N/A"
        else:
            rsi_text = (
                f"{rsi_value:.1f}"
            )

        if sma_value is None:
            sma_text = "N/A"
        else:
            sma_text = (
                "JUU"
                if price >= sma_value
                else "CHINI"
            )

        if reaction:
            zone_text = (
                f"{reaction['type']} @ "
                f"{reaction['level']:.4f}"
            )
        else:
            zone_text = (
                "Hakuna reaction zone "
                "ya karibu"
            )

        sweep_text = (
            "SSL TAKEN"
            if sweep == "low"
            else "BSL TAKEN"
            if sweep == "high"
            else "NONE"
        )

        message = (
            f"{icon} "
            f"<b>ADVISORY SIGNAL: {action}</b>\n\n"
            f"📡 Feed: "
            f"<b>{self.feed_label}</b>\n"
            f"📌 Deriv: "
            f"<b>{self.symbol}</b>\n"
            f"📍 MT5: "
            f"<b>{self.display_name}</b>\n"
            f"⭐ Quality: "
            f"<b>{adaptive_quality}/100 "
            f"— {confidence}</b>\n"
            f"🧠 Learning: "
            f"<b>{learning['decision']}</b> "
            f"(samples={learning['samples']}, "
            f"adjust={adaptive_adjustment:+d})\n\n"
            f"💰 Entry: "
            f"<b>{price:.4f}</b>\n"
            f"🎯 TP: "
            f"<b>{tp:.4f}</b>\n"
            f"🛑 SL: "
            f"<b>{sl:.4f}</b>\n"
            f"⚖️ R:R: "
            f"<b>1:{rr:.2f}</b>\n"
            f"📊 Lot: "
            f"<b>{lot or 'N/A'}</b>\n\n"
            f"🌍 1D Bias: "
            f"<b>{tf(self.d1)}</b>\n"
            f"🧠 15M: "
            f"<b>{tf(self.m15)}</b>\n"
            f"🔄 5M: "
            f"<b>{tf(self.m5)}</b>\n"
            f"⚡ 1M Entry: "
            f"<b>{timing_reason}</b>\n\n"
            f"📍 Reaction Zone: "
            f"<b>{zone_text}</b>\n"
            f"💧 Liquidity: "
            f"<b>{sweep_text}</b>\n"
            f"🌐 Regime: "
            f"<b>{regime}</b>\n"
            f"⚡ Movement: "
            f"<b>{movement_direction or 'N/A'}</b> | "
            f"Volatility: "
            f"<b>{volatility}</b> "
            f"({movement.get('range_ratio', 0):.2f}x)\n"
            f"📊 RSI({RSI_PERIOD}): "
            f"<b>{rsi_text}</b>\n"
            f"📏 SMA{SMA_TREND}: "
            f"<b>{sma_text}</b>\n"
            f"🧩 Setup: "
            f"<b>{setup.get('reason', 'SMC')}</b>\n"
            f"🧠 Feedback: "
            f"<b>{learning['decision']}</b>\n"
            f"📝 Reasons: "
            f"<b>{', '.join(reasons)}</b>\n\n"
            "⚠️ <i>Advisory only. "
            "Bot hai-trade.</i>"
        )

        try:
            await self.telegram.send(
                message
            )

            signal_data = {
                **features,
                "confidence": confidence,
                "quality": adaptive_quality,
                "base_quality": quality,
                "learning_adjustment":
                    adaptive_adjustment,
                "learning_samples":
                    learning["samples"],
                "learning_win_rate":
                    learning["win_rate"],
                "reaction_level":
                    (
                        reaction["level"]
                        if reaction
                        else None
                    ),
                "entry": price,
                "tp": tp,
                "sl": sl,
                "rr": rr,
                "entry_epoch": int(
                    candle.get(
                        "epoch",
                        time.time(),
                    )
                ),
            }

            event_id = (
                self.memory.record_signal(
                    self.symbol,
                    self.feed_label,
                    self.display_name,
                    signal_data,
                )
            )

            registered = (
                self.tracker.register(
                    self.symbol,
                    self.feed_label,
                    direction,
                    price,
                    tp,
                    sl,
                    self.display_name,
                )
            )

            if not registered:
                log.warning(
                    "[%s | %s] Tracker rejected "
                    "after signal send.",
                    self.display_name,
                    self.feed_label,
                )
                return

            self.active_event_id = (
                event_id
            )

            self.active_learning_key = (
                await self.learner.register_signal(
                    signal_data
                )
            )

            self.last_signal_time = now

            log.info(
                "[%s | %s] SIGNAL %s | "
                "base=%s adaptive=%s "
                "learning=%s samples=%s "
                "entry=%.4f sl=%.4f tp=%.4f "
                "rr=%.2f",
                self.display_name,
                self.feed_label,
                action,
                quality,
                adaptive_quality,
                learning["decision"],
                learning["samples"],
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
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN na "
            "TELEGRAM_CHAT_ID lazima ziwekwe."
        )

    telegram = TelegramNotifier(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )

    tracker = TradeTracker()
    memory = SymbolMemory()
    learner = AdaptiveLearningEngine()

    client = PublicMarketClient(
        timeout=20
    )

    await client.connect()

    monitors = [
        PairMonitor(
            *item,
            telegram,
            tracker,
            memory,
            learner,
        )
        for item in SYMBOLS
    ]

    for monitor in monitors:
        await monitor.initialize(
            client
        )

    async def callback(
        symbol,
        candle,
    ):
        for monitor in monitors:
            if monitor.symbol == symbol:
                await monitor.on_candle(
                    symbol,
                    candle,
                )
                return

    client.on_candle = callback

    for monitor in monitors:
        try:
            await client.subscribe_candles(
                monitor.symbol,
                granularity=60,
            )

            log.info(
                "[%s | %s] "
                "Live M1 stream started.",
                monitor.display_name,
                monitor.feed_label,
            )

            await asyncio.sleep(
                0.5
            )

        except Exception as exc:
            log.exception(
                "[%s | %s] Stream start error: %s",
                monitor.display_name,
                monitor.feed_label,
                exc,
            )

    await telegram.send(
        "🤖 <b>Volatility Advisory Engine</b>\n\n"
        "📌 <b>Active feeds:</b>\n"
        "• Volatility 100 (2s)\n"
        "• Volatility 100 (1s)\n"
        "• Volatility 50 (2s)\n\n"
        "🌍 <b>1D:</b> Daily bias ONLY\n"
        "🧠 <b>15M:</b> Context + previous high/low zones\n"
        "🔄 <b>5M:</b> Setup confirmation\n"
        "⚡ <b>1M:</b> Entry timing\n\n"
        "📍 Previous highs/lows = reaction areas\n"
        "💧 Liquidity sweep = confirmation, not the whole strategy\n"
        "🧩 FVG = optional confluence, not mandatory\n"
        "🧠 Feedback learning = ACTIVE and affects future setup confidence\n"
        "🔒 Same feed active-trade protection = "
        f"{'ON' if BLOCK_SAME_FEED_WHILE_ACTIVE else 'OFF'}\n"
        "🔓 Global signal lock = OFF\n\n"
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
        log.exception(
            "Fatal error: %s",
            exc,
        )
