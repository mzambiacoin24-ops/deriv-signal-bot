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
    "POINT_VALUES", "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1"
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
        "granularity": c.get("granularity"),
        "is_new_candle": bool(c.get("is_new_candle", False)),
    }


class GlobalSignalLock:
    """One active signal for the whole bot process."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.active = None

    async def get_active(self):
        async with self._lock:
            return dict(self.active) if self.active is not None else None

    async def reserve(
        self,
        symbol,
        display_name,
        direction,
        entry,
        tp,
        sl,
        signal_epoch,
    ):
        async with self._lock:
            if self.active is not None:
                return False

            self.active = {
                "symbol": symbol,
                "display_name": display_name,
                "direction": direction,
                "entry": float(entry),
                "tp": float(tp),
                "sl": float(sl),
                "signal_epoch": signal_epoch,
            }
            log.info(
                "[GLOBAL LOCK] ACTIVE -> %s %s | Entry %.4f | TP %.4f | SL %.4f",
                display_name,
                direction.upper(),
                entry,
                tp,
                sl,
            )
            return True

    async def release(self):
        async with self._lock:
            old = self.active
            self.active = None
            if old:
                log.info(
                    "[GLOBAL LOCK] RELEASED -> %s %s",
                    old["display_name"],
                    old["direction"].upper(),
                )
            return old

    async def check_and_close(self, symbol, candle):
        async with self._lock:
            if self.active is None or symbol != self.active["symbol"]:
                return None

            active = dict(self.active)
            signal_epoch = active.get("signal_epoch")
            candle_epoch = candle.get("epoch")

            # Never evaluate the candle that created the signal.
            if signal_epoch is not None and candle_epoch is not None:
                try:
                    if float(candle_epoch) <= float(signal_epoch):
                        return None
                except (TypeError, ValueError):
                    return None

            try:
                high = float(candle["high"])
                low = float(candle["low"])
            except (TypeError, ValueError, KeyError):
                return None

            if active["direction"] == "up":
                tp_hit = high >= active["tp"]
                sl_hit = low <= active["sl"]
            else:
                tp_hit = low <= active["tp"]
                sl_hit = high >= active["sl"]

            if not tp_hit and not sl_hit:
                return None

            if tp_hit and sl_hit:
                result = "AMBIGUOUS"
                hit_price = None
            elif tp_hit:
                result = "TP"
                hit_price = active["tp"]
            else:
                result = "SL"
                hit_price = active["sl"]

            self.active = None
            log.info(
                "[GLOBAL LOCK] %s -> %s %s | Entry %.4f | TP %.4f | SL %.4f",
                result,
                active["display_name"],
                active["direction"].upper(),
                active["entry"],
                active["tp"],
                active["sl"],
            )
            return {
                **active,
                "result": result,
                "hit_price": hit_price,
                "candle_epoch": candle_epoch,
            }


class PairMonitor:
    def __init__(
        self,
        primary_symbol,
        secondary_symbol,
        display_name,
        telegram,
        signal_lock,
    ):
        self.primary_symbol = primary_symbol
        self.secondary_symbol = secondary_symbol
        self.display_name = display_name
        self.telegram = telegram
        self.signal_lock = signal_lock

        self.htf = SMCAnalyzer(primary_symbol)
        self.ltf = SMCAnalyzer(primary_symbol)
        self.ltf_secondary = SMCAnalyzer(secondary_symbol)

        self.ltf_closes = deque(maxlen=max(RSI_PERIOD, SMA_TREND) + 5)
        self.point_value = POINT_VALUES.get(primary_symbol)

        # Explicit signal gate. A candle may update many times because the
        # Deriv tick stream sends every tick. Signal generation is allowed
        # only once for a given candle epoch.
        self._last_signal_candle_epoch = None

    async def on_candle(self, symbol, ohlc):
        try:
            granularity = int(ohlc.get("granularity", 0))
        except (TypeError, ValueError):
            return

        c = _to_ohlc(ohlc)

        if symbol == self.primary_symbol and granularity == HTF_GRANULARITY:
            self.htf.add_candle(c)
            return

        if symbol == self.primary_symbol and granularity == LTF_GRANULARITY:
            # TP/SL must still be checked on EVERY tick so a hit inside a
            # candle is not missed.
            result = await self.signal_lock.check_and_close(
                self.primary_symbol, c
            )
            if result:
                await self._notify_signal_result(result)
                # Do not create a new signal on the same candle that closed
                # the previous signal. Wait for the next candle.
                return

            # Keep the live candle updated for SMC/indicators. SMCAnalyzer
            # replaces the existing candle when epoch is unchanged instead
            # of appending another candle.
            self.ltf_closes.append(c["close"])
            entry = self.ltf.add_candle(c)

            if not entry:
                return

            signal_epoch = entry.get("epoch")
            if signal_epoch is None:
                log.warning(
                    "[%s] Signal imekataliwa: candle haina epoch ya kudeduplicate.",
                    self.display_name,
                )
                return

            # HARD PER-CANDLE GATE. Even if another code path produces the
            # same entry again, this candle can never send twice.
            if signal_epoch == self._last_signal_candle_epoch:
                log.info(
                    "[%s] Signal duplicate imezuiwa: candle epoch=%s tayari ilishatumika.",
                    self.display_name,
                    signal_epoch,
                )
                return

            sent = await self._maybe_send_signal(
                entry,
                c["close"],
                signal_epoch,
            )
            if sent:
                self._last_signal_candle_epoch = signal_epoch
            return

        if symbol == self.secondary_symbol and granularity == LTF_GRANULARITY:
            self.ltf_secondary.add_candle(c)

    async def _notify_signal_result(self, result):
        result_type = result["result"]
        direction_text = "BUY" if result["direction"] == "up" else "SELL"

        if result_type == "TP":
            text = (
                "🎯 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🎯 Take Profit: <b>HIT</b> @ {result['hit_price']:.4f}\n\n"
                "🔓 <b>GLOBAL LOCK: RELEASED</b>\n"
                "Bot sasa inaruhusiwa kutafuta signal mpya."
            )
        elif result_type == "SL":
            text = (
                "🛑 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🛑 Stop Loss: <b>HIT</b> @ {result['hit_price']:.4f}\n\n"
                "🔓 <b>GLOBAL LOCK: RELEASED</b>\n"
                "Bot sasa inaruhusiwa kutafuta signal mpya."
            )
        else:
            text = (
                "⚠️ <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                "⚠️ TP na SL zote ziliguswa ndani ya candle moja.\n"
                "Haiwezekani kujua ni ipi iligongwa kwanza kwa OHLC pekee.\n\n"
                "🔓 <b>GLOBAL LOCK: RELEASED</b>"
            )

        try:
            await self.telegram.send(text)
        except Exception as e:
            log.error(
                "[%s] Imeshindikana kutuma taarifa ya TP/SL: %s",
                self.display_name,
                e,
            )

    async def _maybe_send_signal(self, entry, price, signal_epoch):
        active = await self.signal_lock.get_active()
        if active is not None:
            log.info(
                "[%s] SIGNAL IMEZUIWA: GLOBAL LOCK ACTIVE kwa %s %s.",
                self.display_name,
                active["display_name"],
                active["direction"].upper(),
            )
            return False

        direction = entry["direction"]
        ob = entry["ob"]

        if self.htf.trend is None or self.htf.trend != direction:
            return False

        rsi_val = rsi(self.ltf_closes, RSI_PERIOD)
        sma_val = sma(self.ltf_closes, SMA_TREND)
        if rsi_val is None or sma_val is None:
            return False

        if direction == "up" and (rsi_val >= RSI_OVERBOUGHT or price < sma_val):
            return False
        if direction == "down" and (rsi_val <= RSI_OVERSOLD or price > sma_val):
            return False

        primary_swept = self.ltf.last_sweep
        secondary_swept = self.ltf_secondary.last_sweep

        if primary_swept is None:
            smt_note = (
                "ℹ️ Hakuna liquidity sweep dhahiri kabla ya CHoCH hii - "
                "SMT haikupimwa kwa ishara hii."
            )
        elif primary_swept == secondary_swept:
            smt_note = (
                "⚠️ Hakuna SMT divergence (pacha wa (1s) alifanya sweep ile ile - "
                "uthibitisho dhaifu)."
            )
        else:
            smt_note = (
                "✅ SMT divergence imethibitika (pacha wa (1s) HAKUFANYA sweep - "
                "uthibitisho mzuri)."
            )

        buffer = price * (SL_BUFFER_PCT / 100)
        sl_price = ob["low"] - buffer if direction == "up" else ob["high"] + buffer
        sl_distance = abs(price - sl_price)
        if sl_distance <= 0:
            return False

        tp_price = (
            price + RR_RATIO * sl_distance
            if direction == "up"
            else price - RR_RATIO * sl_distance
        )

        if self.point_value and self.point_value > 0:
            risk_amount = ACCOUNT_BALANCE * (RISK_PERCENT_PER_TRADE / 100)
            lot = risk_amount / (sl_distance * self.point_value)
            lot = max(round(lot, 2), 0.01)
            lot_line = f"📊 Lot Size (pendekezo): <b>{lot}</b>\n"
        else:
            lot_line = (
                "📊 Lot Size: weka POINT_VALUES ya symbol hii kwenye .env "
                "(angalia MT5 -> Specification) kupata pendekezo\n"
            )

        emoji = "📈" if direction == "up" else "📉"
        action = "NUNUA (BUY)" if direction == "up" else "UZA (SELL)"

        # Reserve BEFORE Telegram send. This is atomic inside this process.
        reserved = await self.signal_lock.reserve(
            symbol=self.primary_symbol,
            display_name=self.display_name,
            direction=direction,
            entry=price,
            tp=tp_price,
            sl=sl_price,
            signal_epoch=signal_epoch,
        )
        if not reserved:
            return False

        try:
            await self.telegram.send(
                f"{emoji} <b>ISHARA: {action}</b>\n"
                f"Symbol (MT5): <b>{self.display_name}</b>\n"
                f"Bei ya kuingia: {price:.4f}\n"
                f"🎯 Take Profit: {tp_price:.4f}\n"
                f"🛑 Stop Loss: {sl_price:.4f}\n"
                f"{lot_line}"
                f"Muundo: HTF(15m) bias={self.htf.trend.upper()} + LTF(1m) CHoCH+OB retest\n"
                f"RSI(14): {rsi_val:.1f} | Bei dhidi ya SMA{SMA_TREND}: "
                f"{'juu' if price > sma_val else 'chini'}\n"
                f"{smt_note}\n\n"
                "🔒 <b>GLOBAL LOCK: ACTIVE</b>\n"
                "Hakuna signal nyingine itakayotumwa mpaka signal hii ifike TP au SL.\n\n"
                "⚠️ Hii ni PENDEKEZO TU (si ushauri wa kifedha) - "
                "fanya uamuzi wako mwenyewe kabla ya kubonyeza kwenye MT5."
            )
        except Exception:
            await self.signal_lock.release()
            raise

        log.info(
            "[%s] ISHARA %s IMETUMWA | candle_epoch=%s | Entry %.4f | TP %.4f | SL %.4f",
            self.display_name,
            direction.upper(),
            signal_epoch,
            price,
            tp_price,
            sl_price,
        )
        return True


async def run_pair(monitor):
    client = PublicMarketClient()
    client.on_candle = monitor.on_candle

    backoff = 5
    max_backoff = 300

    while True:
        started_at = time.time()
        try:
            await client.connect()

            htf_hist = await client.get_candle_history(
                monitor.primary_symbol, HTF_GRANULARITY, CANDLE_COUNT
            )
            for c in htf_hist:
                monitor.htf.add_candle(_to_ohlc(c))
            await client.subscribe_candles(monitor.primary_symbol, HTF_GRANULARITY)

            ltf_hist = await client.get_candle_history(
                monitor.primary_symbol, LTF_GRANULARITY, CANDLE_COUNT
            )
            for c in ltf_hist:
                cc = _to_ohlc(c)
                monitor.ltf_closes.append(cc["close"])
                monitor.ltf.add_candle(cc)
            await client.subscribe_candles(monitor.primary_symbol, LTF_GRANULARITY)

            sec_hist = await client.get_candle_history(
                monitor.secondary_symbol, LTF_GRANULARITY, CANDLE_COUNT
            )
            for c in sec_hist:
                monitor.ltf_secondary.add_candle(_to_ohlc(c))
            await client.subscribe_candles(monitor.secondary_symbol, LTF_GRANULARITY)

            log.info(
                "[%s] Historia imepakiwa (HTF+LTF+pacha), inasubiri candles mpya...",
                monitor.display_name,
            )
            await client.wait_until_disconnected()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            connected_duration = time.time() - started_at
            if connected_duration > 120:
                backoff = 5
            log.error("[%s] Muunganiko umekatika: %s", monitor.display_name, e)
            try:
                await client.close()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def acquire_process_lock():
    """Prevent two bot processes on the same Linux host."""
    try:
        import fcntl
    except ImportError:
        log.warning("fcntl haipo; process-level singleton lock haijawezeshwa.")
        return None

    path = os.getenv("SIGNAL_BOT_LOCK_FILE", "/tmp/deriv_signal_bot.lock")
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            "Signal bot tayari ina-run kwenye host hii. "
            "Instance ya pili imezuiwa ili kuzuia duplicate signals."
        )
    return handle


async def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit(
            "Weka TELEGRAM_BOT_TOKEN na TELEGRAM_CHAT_ID kwenye .env."
        )

    process_lock = await acquire_process_lock()
    try:
        telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        signal_lock = GlobalSignalLock()

        names = ", ".join(p[2] for p in SYMBOL_PAIRS)
        await telegram.send(
            f"🤖 <b>Signal Bot v2 imeanza (SMC/SMT + HTF/LTF + RSI/SMA)</b>\n"
            f"Symbols: {names}\n"
            f"HTF bias: {HTF_GRANULARITY}s | LTF entry: {LTF_GRANULARITY}s\n\n"
            "🔒 <b>GLOBAL SIGNAL LOCK: READY</b>\n"
            "Signal moja tu inaweza kuwa ACTIVE kwa wakati mmoja.\n"
            "Signal mpya itaruhusiwa baada ya TP au SL ya signal iliyopo kugunduliwa.\n\n"
            "🕯️ <b>CANDLE DEDUPE: ACTIVE</b>\n"
            "Ticks nyingi za candle moja hazitaruhusiwa kutengeneza signal nyingi.\n\n"
            "⚠️ Hii HAITRADE - inatuma mapendekezo (Entry/TP/SL/Lot) TU."
        )

        monitors = [
            PairMonitor(primary, secondary, display, telegram, signal_lock)
            for primary, secondary, display in SYMBOL_PAIRS
        ]

        await asyncio.gather(*(run_pair(m) for m in monitors))
    finally:
        if process_lock is not None:
            try:
                import fcntl
                fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            process_lock.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        log.error("%s", exc)
        raise SystemExit(1)
