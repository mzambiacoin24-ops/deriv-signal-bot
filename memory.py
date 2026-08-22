import json
import os
import tempfile
from datetime import datetime, timezone


MEMORY_FILE = os.getenv(
    "SYMBOL_MEMORY_FILE",
    "symbol_memory.json",
)

MAX_EVENTS_PER_SYMBOL = int(
    os.getenv(
        "MAX_MEMORY_EVENTS_PER_SYMBOL",
        "500",
    )
)


class SymbolMemory:
    """
    Persistent memory for each Deriv Volatility symbol/feed.

    Memory is not just a log:
    - stores every advisory signal and result
    - learns which setups work on each symbol/feed
    - separates 1s/2s behaviour
    - tracks reaction-zone performance
    - provides pattern statistics that the signal engine can
      consult before sending future signals
    """

    def __init__(
        self,
        path=MEMORY_FILE,
        max_events=MAX_EVENTS_PER_SYMBOL,
    ):
        self.path = path
        self.max_eventsts = max(
            1,
            int(max_events),
        )
        self.data = self._load()

    # =========================================================
    # STORAGE
    # =========================================================

    def _empty(self):
        return {
            "version": 3,
            "symbols": {},
        }

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()

        try:
            with open(
                self.path,
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            if not isinstance(data, dict):
                return self._empty()

            if not isinstance(
                data.get("symbols"),
                dict,
            ):
                data["symbols"] = {}

            data.setdefault(
                "version",
                3,
            )

            return data

        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return self._empty()

    def _save(self):
        directory = os.path.dirname(
            os.path.abspath(self.path)
        )

        os.makedirs(
            directory,
            exist_ok=True,
        )

        fd, temp_path = tempfile.mkstemp(
            prefix="symbol_memory_",
            suffix=".tmp",
            dir=directory,
            text=True,
        )

        try:
            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    self.data,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.write("\n")

            os.replace(
                temp_path,
                self.path,
            )

        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _now():
        return datetime.now(
            timezone.utc
        ).isoformat()

    # =========================================================
    # SYMBOL STATE
    # =========================================================

    def _ensure_symbol(
        self,
        symbol,
        feed_label=None,
        display_name=None,
    ):
        key = str(symbol)

        symbols = self.data[
            "symbols"
        ]

        if (
            key not in symbols
            or not isinstance(
                symbols[key],
                dict,
            )
        ):
            symbols[key] = {
                "symbol": key,
                "feed": feed_label,
                "display_name": display_name,
                "stats": {
                    "signals": 0,
                    "wins": 0,
                    "losses": 0,
                    "pending": 0,
                    "buy": 0,
                    "sell": 0,
                    "tp_rate": 0.0,
                },
                "patterns": {},
                "events": [],
                "updated_at": self._now(),
            }

        item = symbols[key]

        if feed_label is not None:
            item["feed"] = feed_label

        if display_name is not None:
            item["display_name"] = display_name

        item.setdefault(
            "stats",
            {},
        )
        item.setdefault(
            "patterns",
            {},
        )
        item.setdefault(
            "events",
            [],
        )
        item.setdefault(
            "updated_at",
            self._now(),
        )

        return item

    # =========================================================
    # SIGNAL MEMORY
    # =========================================================

    def record_signal(
        self,
        symbol,
        feed_label,
        display_name,
        signal,
    ):
        item = self._ensure_symbol(
            symbol,
            feed_label,
            display_name,
        )

        stats = item["stats"]

        stats["signals"] = int(
            stats.get(
                "signals",
                0,
            )
        ) + 1

        stats["pending"] = int(
            stats.get(
                "pending",
                0,
            )
        ) + 1

        direction = str(
            signal.get(
                "direction",
                "",
            )
        ).lower()

        if direction == "up":
            stats["buy"] = int(
                stats.get(
                    "buy",
                    0,
                )
            ) + 1

        elif direction == "down":
            stats["sell"] = int(
                stats.get(
                    "sell",
                    0,
                )
            ) + 1

        event_id = (
            f"{symbol}:"
            f"{feed_label}:"
            f"{int(signal.get('entry_epoch', 0))}:"
            f"{len(item['events']) + 1}"
        )

        event = {
            "id": event_id,
            "type": "signal",
            "created_at": self._now(),
            "status": "pending",

            "symbol": str(symbol),
            "feed": str(feed_label),
            "display_name": display_name,

            "direction": direction,
            "entry": signal.get("entry"),
            "tp": signal.get("tp"),
            "sl": signal.get("sl"),
            "rr": signal.get("rr"),

            "quality": signal.get("quality"),
            "confidence": signal.get(
                "confidence"
            ),

            "setup": signal.get(
                "setup"
            ),
            "sweep": signal.get(
                "sweep"
            ),

            "regime": signal.get(
                "regime"
            ),
            "location": signal.get(
                "location"
            ),

            "d1": signal.get("d1"),
            "h4": signal.get("h4"),
            "m30": signal.get("m30"),
            "m15": signal.get("m15"),
            "m5": signal.get("m5"),
            "m1": signal.get("m1"),

            "movement_direction": signal.get(
                "movement_direction"
            ),
            "volatility": signal.get(
                "volatility"
            ),
            "movement_state": signal.get(
                "movement_state"
            ),
            "movement_score": signal.get(
                "movement_score"
            ),
            "movement_pressure": signal.get(
                "movement_pressure"
            ),

            "movement_acceleration": signal.get(
                "movement_acceleration"
            ),
            "range_ratio": signal.get(
                "range_ratio"
            ),

            "reaction_level": signal.get(
                "reaction_level"
            ),
            "reaction_type": signal.get(
                "reaction_type"
            ),

            "rsi": signal.get("rsi"),
            "sma": signal.get("sma"),

            "entry_epoch": signal.get(
                "entry_epoch"
            ),

            "result": None,
        }

        item["events"].append(
            event
        )

        self._trim(item)

        item["updated_at"] = self._now()

        self._save()

        return event_id

    # =========================================================
    # RESULT / LEARNING
    # =========================================================

    def record_result(
        self,
        symbol,
        event_id,
        result,
        exit_price=None,
        exit_epoch=None,
    ):
        item = self._ensure_symbol(
            symbol
        )

        target = None

        for event in reversed(
            item["events"]
        ):
            if event.get("id") == event_id:
                target = event
                break

        if (
            target is None
            or target.get("status")
            != "pending"
        ):
            return False

        normalized = str(
            result
        ).lower()

        if normalized not in {
            "tp",
            "sl",
            "cancelled",
            "expired",
        }:
            return False

        target["status"] = normalized
        target["result"] = normalized
        target["exit_price"] = exit_price
        target["exit_epoch"] = exit_epoch
        target["closed_at"] = self._now()

        stats = item["stats"]

        stats["pending"] = max(
            0,
            int(
                stats.get(
                    "pending",
                    0,
                )
            ) - 1,
        )

        if normalized == "tp":
            stats["wins"] = int(
                stats.get(
                    "wins",
                    0,
                )
            ) + 1

        elif normalized == "sl":
            stats["losses"] = int(
                stats.get(
                    "losses",
                    0,
                )
            ) + 1

        closed = (
            int(
                stats.get(
                    "wins",
                    0,
                )
            )
            + int(
                stats.get(
                    "losses",
                    0,
                )
            )
        )

        stats["tp_rate"] = (
            round(
                (
                    int(
                        stats.get(
                            "wins",
                            0,
                        )
                    )
                    / closed
                )
                * 100,
                2,
            )
            if closed
            else 0.0
        )

        self._learn_pattern(
            item,
            target,
        )

        item["updated_at"] = self._now()

        self._save()

        return True

    def _pattern_key(self, event):
        """
        Keep the key focused on behaviour that can actually
        repeat on a Volatility Index.

        Feed is deliberately included because 1s and 2s are
        different feeds and must not share performance blindly.
        """
        values = [
            event.get("feed", "unknown"),
            event.get("direction", "unknown"),
            event.get("setup", "unknown"),
            event.get("sweep", "none") or "none",
            event.get("regime", "unknown"),
            event.get("volatility", "unknown"),
            event.get(
                "reaction_type",
                "unknown",
            ),
        ]

        return "|".join(
            str(value)
            for value in values
        )

    def _learn_pattern(
        self,
        item,
        event,
    ):
        key = self._pattern_key(
            event
        )

        pattern = item[
            "patterns"
        ].setdefault(
            key,
            {
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "tp_rate": 0.0,
                "updated_at": None,
            },
        )

        pattern["signals"] += 1

        if event.get(
            "result"
        ) == "tp":
            pattern["wins"] += 1

        elif event.get(
            "result"
        ) == "sl":
            pattern["losses"] += 1

        closed = (
            pattern["wins"]
            + pattern["losses"]
        )

        pattern["tp_rate"] = (
            round(
                (
                    pattern["wins"]
                    / closed
                )
                * 100,
                2,
            )
            if closed
            else 0.0
        )

        pattern["updated_at"] = (
            self._now()
        )

    # =========================================================
    # MEMORY QUERY
    # =========================================================

    def get_pattern_stats(
        self,
        symbol,
        feed_label,
        direction,
        setup,
        sweep,
        regime,
        volatility,
        reaction_type="unknown",
    ):
        """
        Return historical performance for the exact setup.

        This does NOT automatically block a signal. The signal
        engine decides the minimum sample/win-rate policy.
        """
        item = self.get_symbol(
            symbol
        )

        if not item:
            return {
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "tp_rate": None,
            }

        event = {
            "feed": feed_label,
            "direction": direction,
            "setup": setup,
            "sweep": sweep,
            "regime": regime,
            "volatility": volatility,
            "reaction_type": reaction_type,
        }

        key = self._pattern_key(
            event
        )

        pattern = item.get(
            "patterns",
            {}
        ).get(key)

        if not pattern:
            return {
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "tp_rate": None,
            }

        wins = int(
            pattern.get(
                "wins",
                0,
            )
        )

        losses = int(
            pattern.get(
                "losses",
                0,
            )
        )

        samples = wins + losses

        return {
            "samples": samples,
            "wins": wins,
            "losses": losses,
            "tp_rate": (
                (
                    wins / samples
                ) * 100
                if samples
                else None
            ),
        }

    def recent_results(
        self,
        symbol,
        feed_label=None,
        limit=20,
    ):
        """
        Return recent completed results for this symbol/feed.
        """
        item = self.get_symbol(
            symbol
        )

        if not item:
            return []

        results = []

        for event in reversed(
            item.get(
                "events",
                [],
            )
        ):
            if (
                feed_label is not None
                and event.get("feed")
                != feed_label
            ):
                continue

            if event.get(
                "result"
            ) not in {
                "tp",
                "sl",
            }:
                continue

            results.append(
                dict(event)
            )

            if len(results) >= limit:
                break

        return results

    def get_symbol(
        self,
        symbol,
    ):
        return self.data[
            "symbols"
        ].get(str(symbol))

    def get_stats(
        self,
        symbol,
    ):
        item = self.get_symbol(
            symbol
        )

        return (
            None
            if item is None
            else dict(
                item.get(
                    "stats",
                    {},
                )
            )
        )

    def get_patterns(
        self,
        symbol,
    ):
        item = self.get_symbol(
            symbol
        )

        return (
            {}
            if item is None
            else dict(
                item.get(
                    "patterns",
                    {},
                )
            )
        )

    def _trim(self, item):
        if len(
            item["events"]
        ) > self.max_eventsts:
            item["events"] = item[
                "events"
            ][-self.max_eventsts:]
