import time


class TradeTracker:
    """
    Hufuatilia signal moja iliyo active mpaka TP au SL iguswe.

    Haitengenezi signal mpya na haigusi SMC.
    Inafanya kazi kama layer ya ufuatiliaji tu.
    """

    def __init__(self):
        self._active = {}

    def key(self, symbol, feed_label):
        return f"{symbol}|{feed_label}"

    def is_active(self, symbol, feed_label):
        return self.key(symbol, feed_label) in self._active

    def register(
        self,
        symbol,
        feed_label,
        direction,
        entry,
        tp,
        sl,
        display_name=None,
    ):
        key = self.key(symbol, feed_label)

        if key in self._active:
            return False

        self._active[key] = {
            "symbol": symbol,
            "feed_label": feed_label,
            "display_name": display_name or symbol,
            "direction": direction,
            "entry": float(entry),
            "tp": float(tp),
            "sl": float(sl),
            "started_at": time.time(),
        }

        return True

    def remove(self, symbol, feed_label):
        self._active.pop(
            self.key(symbol, feed_label),
            None,
        )

    def get(self, symbol, feed_label):
        trade = self._active.get(
            self.key(symbol, feed_label)
        )

        if trade is None:
            return None

        return dict(trade)

    def check_price(
        self,
        symbol,
        feed_label,
        price,
    ):
        """
        Angalia bei moja dhidi ya TP/SL.

        Returns:
            None  -> trade bado active
            dict -> trade imefungwa
        """

        key = self.key(symbol, feed_label)

        trade = self._active.get(key)

        if trade is None:
            return None

        price = float(price)
        direction = trade["direction"]
        hit = None

        if direction == "up":
            if price >= trade["tp"]:
                hit = "TP"
            elif price <= trade["sl"]:
                hit = "SL"

        elif direction == "down":
            if price <= trade["tp"]:
                hit = "TP"
            elif price >= trade["sl"]:
                hit = "SL"

        if hit is None:
            return None

        trade["result"] = hit
        trade["exit"] = price
        trade["closed_at"] = time.time()
        trade["duration_seconds"] = (
            trade["closed_at"]
            - trade["started_at"]
        )

        completed = dict(trade)

        self._active.pop(
            key,
            None,
        )

        return completed

    def active_count(self):
        return len(self._active)

    def active_trades(self):
        return [
            dict(trade)
            for trade in self._active.values()
      ]
