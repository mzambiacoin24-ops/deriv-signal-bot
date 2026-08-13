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

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
)

HTF_GRANULARITY = int(
    os.getenv(
        "SIGNAL_HTF_GRANULARITY",
        "900",
    )
)

LTF_GRANULARITY = int(
    os.getenv(
        "SIGNAL_LTF_GRANULARITY",
        "60",
    )
)

CANDLE_COUNT = int(
    os.getenv(
        "SIGNAL_CANDLE_COUNT",
        "200",
    )
)

RSI_PERIOD = int(
    os.getenv(
        "SIGNAL_RSI_PERIOD",
        "14",
    )
)

RSI_OVERBOUGHT = float(
    os.getenv(
        "SIGNAL_RSI_OVERBOUGHT",
        "70",
    )
)

RSI_OVERSOLD = float(
    os.getenv(
        "SIGNAL_RSI_OVERSOLD",
        "30",
    )
)

SMA_TREND = int(
    os.getenv(
        "SIGNAL_SMA_TREND",
        "50",
    )
)

ACCOUNT_BALANCE = float(
    os.getenv(
        "ACCOUNT_BALANCE",
        "10000",
    )
)

RISK_PERCENT_PER_TRADE = float(
    os.getenv(
        "RISK_PERCENT_PER_TRADE",
        "1",
    )
)

RR_RATIO = float(
    os.getenv(
        "RR_RATIO",
        "2",
    )
)

SL_BUFFER_PCT = float(
    os.getenv(
        "SL_BUFFER_PCT",
        "0.1",
    )
)

SMT_MAX_AGE_CANDLES = int(
    os.getenv(
        "SMT_MAX_AGE_CANDLES",
        "5",
    )
)


POINT_VALUES = {}

for _pair in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1",
).split(","):

    if "=" in _pair:

        _sym, _val = _pair.split(
            "=",
            1,
        )

        try:
            POINT_VALUES[
                _sym.strip()
            ] = float(
                _val.strip()
            )

        except ValueError:
            pass


SYMBOL_PAIRS = [
    (
        "R_10",
        "1HZ10V",
        "Volatility 10 Index",
    ),
    (
        "R_25",
        "1HZ25V",
        "Volatility 25 Index",
    ),
    (
        "R_50",
        "1HZ50V",
        "Volatility 50 Index",
    ),
    (
        "R_75",
        "1HZ75V",
        "Volatility 75 Index",
    ),
    (
        "R_100",
        "1HZ100V",
        "Volatility 100 Index",
    ),
]


def _to_ohlc(c):

    return {
        "open": float(c["open"]),
        "high": float(c["high"]),
        "low": float(c["low"]),
        "close": float(c["close"]),
        "epoch": c.get("epoch"),
        "granularity": c.get(
            "granularity"
        ),
        "is_new_candle": bool(
            c.get(
                "is_new_candle",
                False,
            )
        ),
    }


class SignalTracker:

    """
    Inafuatilia TP/SL za signals zote.

    Hakuna GLOBAL SIGNAL LOCK.

    Signal mpya inaweza kutumwa hata kama
    signal nyingine bado iko ACTIVE.
    """

    def __init__(self):

        self._lock = asyncio.Lock()

        self.active_signals = []

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

            signal = {
                "symbol": symbol,
                "display_name": display_name,
                "direction": direction,
                "entry": float(entry),
                "tp": float(tp),
                "sl": float(sl),
                "signal_epoch": signal_epoch,
                "created_at": time.time(),
            }

            self.active_signals.append(
                signal
            )

            log.info(
                "[TRACKER] ADD -> %s %s | "
                "Entry %.4f | TP %.4f | SL %.4f | "
                "Active=%d",
                display_name,
                direction.upper(),
                entry,
                tp,
                sl,
                len(self.active_signals),
            )

            return True

    async def remove_signal(
        self,
        signal_epoch,
        symbol,
    ):

        async with self._lock:

            self.active_signals = [
                signal
                for signal in self.active_signals
                if not (
                    signal["signal_epoch"]
                    == signal_epoch
                    and signal["symbol"]
                    == symbol
                )
            ]

    async def check_and_close(
        self,
        symbol,
        candle,
    ):

        async with self._lock:

            if not self.active_signals:
                return []

            candle_epoch = candle.get(
                "epoch"
            )

            try:

                high = float(
                    candle["high"]
                )

                low = float(
                    candle["low"]
                )

            except (
                TypeError,
                ValueError,
                KeyError,
            ):

                return []

            results = []

            remaining = []

            for active in self.active_signals:

                if (
                    active["symbol"]
                    != symbol
                ):

                    remaining.append(
                        active
                    )

                    continue

                signal_epoch = active.get(
                    "signal_epoch"
                )

                # Do not evaluate the candle
                # that created the signal.
                if (
                    signal_epoch is not None
                    and candle_epoch is not None
                ):

                    try:

                        if (
                            float(candle_epoch)
                            <= float(signal_epoch)
                        ):

                            remaining.append(
                                active
                            )

                            continue

                    except (
                        TypeError,
                        ValueError,
                    ):

                        remaining.append(
                            active
                        )

                        continue

                if (
                    active["direction"]
                    == "up"
                ):

                    tp_hit = (
                        high
                        >= active["tp"]
                    )

                    sl_hit = (
                        low
                        <= active["sl"]
                    )

                else:

                    tp_hit = (
                        low
                        <= active["tp"]
                    )

                    sl_hit = (
                        high
                        >= active["sl"]
                    )

                if not tp_hit and not sl_hit:

                    remaining.append(
                        active
                    )

                    continue

                if tp_hit and sl_hit:

                    result = "AMBIGUOUS"

                    hit_price = None

                elif tp_hit:

                    result = "TP"

                    hit_price = active["tp"]

                else:

                    result = "SL"

                    hit_price = active["sl"]

                results.append(
                    {
                        **active,
                        "result": result,
                        "hit_price": hit_price,
                        "candle_epoch": candle_epoch,
                    }
                )

                log.info(
                    "[TRACKER] %s -> %s %s | "
                    "Entry %.4f | TP %.4f | SL %.4f",
                    result,
                    active[
                        "display_name"
                    ],
                    active[
                        "direction"
                    ].upper(),
                    active["entry"],
                    active["tp"],
                    active["sl"],
                )

            self.active_signals = remaining

            return results


class PairMonitor:

    def __init__(
        self,
        primary_symbol,
        secondary_symbol,
        display_name,
        telegram,
        signal_tracker,
    ):

        self.primary_symbol = (
            primary_symbol
        )

        self.secondary_symbol = (
            secondary_symbol
        )

        self.display_name = (
            display_name
        )

        self.telegram = telegram

        self.signal_tracker = (
            signal_tracker
        )

        self.htf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf_secondary = (
            SMCAnalyzer(
                secondary_symbol
            )
        )

        self.ltf_closes = deque(
            maxlen=max(
                RSI_PERIOD,
                SMA_TREND,
            ) + 5
        )

        self.point_value = (
            POINT_VALUES.get(
                primary_symbol
            )
        )

        self._last_signal_candle_epoch = (
            None
        )

    async def on_candle(
        self,
        symbol,
        ohlc,
    ):

        try:

            granularity = int(
                ohlc.get(
                    "granularity",
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return

        c = _to_ohlc(ohlc)

        # ============================================================
        # HTF
        # ============================================================

        if (
            symbol
            == self.primary_symbol
            and granularity
            == HTF_GRANULARITY
        ):

            self.htf.add_candle(c)

            return

        # ============================================================
        # PRIMARY LTF
        # ============================================================

        if (
            symbol
            == self.primary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            # --------------------------------------------------------
            # TP / SL TRACKING
            # --------------------------------------------------------

            results = (
                await self.signal_tracker.check_and_close(
                    self.primary_symbol,
                    c,
                )
            )

            for result in results:

                await self._notify_signal_result(
                    result
                )

            # --------------------------------------------------------
            # UPDATE LTF DATA
            # --------------------------------------------------------

            self.ltf_closes.append(
                c["close"]
            )

            entry = self.ltf.add_candle(c)

            if not entry:

                return

            signal_epoch = entry.get(
                "epoch"
            )

            if signal_epoch is None:

                return

            # --------------------------------------------------------
            # CANDLE DEDUPE
            # --------------------------------------------------------

            if (
                signal_epoch
                == self._last_signal_candle_epoch
            ):

                log.info(
                    "[%s] Duplicate candle "
                    "signal imezuiwa: %s",
                    self.display_name,
                    signal_epoch,
                )

                return

            # --------------------------------------------------------
            # QUALITY FILTER + SIGNAL
            # --------------------------------------------------------

            sent = (
                await self._maybe_send_signal(
                    entry,
                    c["close"],
                    signal_epoch,
                )
            )

            if sent:

                self._last_signal_candle_epoch = (
                    signal_epoch
                )

            return

        # ============================================================
        # SECONDARY
        # ============================================================

        if (
            symbol
            == self.secondary_symbol
            and granularity
            == LTF_GRANULARITY
        ):

            self.ltf_secondary.add_candle(
                c
            )

    # ================================================================
    # TP / SL NOTIFICATION
    # ================================================================

    async def _notify_signal_result(
        self,
        result,
    ):

        direction_text = (
            "BUY"
            if result["direction"]
            == "up"
            else "SELL"
        )

        if result["result"] == "TP":

            text = (
                "🎯 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🎯 Take Profit: <b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                "✅ Signal imefikia TP.\n"
                "Bot inaendelea kutafuta signals."
            )

        elif result["result"] == "SL":

            text = (
                "🛑 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🛑 Stop Loss: <b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                "⚠️ Signal imefikia SL.\n"
                "Bot inaendelea kutafuta signals."
            )

        else:

            text = (
                "⚠️ <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                "⚠️ TP na SL zote ziliguswa "
                "ndani ya candle moja.\n\n"
                "Bot inaendelea kutafuta signals."
            )

        try:

            await self.telegram.send(
                text
            )

        except Exception as e:

            log.error(
                "[%s] TP/SL notification failed: %s",
                self.display_name,
                e,
            )

    # ================================================================
    # SIGNAL QUALITY
    # ================================================================

    async def _maybe_send_signal(
        self,
        entry,
        price,
        signal_epoch,
    ):

        direction = entry[
            "direction"
        ]

        ob = entry["ob"]

        # ============================================================
        # 1. HTF BIAS
        # ============================================================

        if (
            self.htf.trend is None
            or self.htf.trend
            != direction
        ):

            log.info(
                "[%s] QUALITY FAIL -> HTF bias.",
                self.display_name,
            )

            return False

        # ============================================================
        # 2. RSI
        # ============================================================

        rsi_val = rsi(
            self.ltf_closes,
            RSI_PERIOD,
        )

        # ============================================================
        # 3. SMA
        # ============================================================

        sma_val = sma(
            self.ltf_closes,
            SMA_TREND,
        )

        if (
            rsi_val is None
            or sma_val is None
        ):

            return False

        if direction == "up":

            if (
                rsi_val
                >= RSI_OVERBOUGHT
            ):

                log.info(
                    "[%s] QUALITY FAIL -> RSI overbought.",
                    self.display_name,
                )

                return False

            if price < sma_val:

                log.info(
                    "[%s] QUALITY FAIL -> Price below SMA.",
                    self.display_name,
                )

                return False

        else:

            if (
                rsi_val
                <= RSI_OVERSOLD
            ):

                log.info(
                    "[%s] QUALITY FAIL -> RSI oversold.",
                    self.display_name,
                )

                return False

            if price > sma_val:

                log.info(
                    "[%s] QUALITY FAIL -> Price above SMA.",
                    self.display_name,
                )

                return False

        # ============================================================
        # 4. SMT MUST PASS
        # ============================================================

        required_sweep = (
            "low"
            if direction == "up"
            else "high"
        )

        primary_sweep_epoch = (
            self.ltf.get_sweep_epoch(
                required_sweep
            )
        )

        if primary_sweep_epoch is None:

            log.info(
                "[%s] QUALITY FAIL -> "
                "No primary liquidity sweep.",
                self.display_name,
            )

            return False

        # Sweep lazima iwe fresh.
        try:

            sweep_age = (
                float(signal_epoch)
                - float(
                    primary_sweep_epoch
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return False

        max_age_seconds = (
            SMT_MAX_AGE_CANDLES
            * LTF_GRANULARITY
        )

        if (
            sweep_age < 0
            or sweep_age
            > max_age_seconds
        ):

            log.info(
                "[%s] QUALITY FAIL -> "
                "Primary sweep ni ya zamani.",
                self.display_name,
            )

            return False

        # ============================================================
        # SECONDARY MUST NOT MAKE THE SAME SWEEP
        # ============================================================

        secondary_sweep_epoch = (
            self.ltf_secondary.get_sweep_epoch(
                required_sweep
            )
        )

        if secondary_sweep_epoch is not None:

            try:

                secondary_age = (
                    float(signal_epoch)
                    - float(
                        secondary_sweep_epoch
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                secondary_age = (
                    max_age_seconds
                    + 1
                )

            # If secondary made the same-side
            # sweep around the same setup,
            # SMT fails.
            if (
                secondary_age >= 0
                and secondary_age
                <= max_age_seconds
            ):

                log.info(
                    "[%s] QUALITY FAIL -> "
                    "Secondary pair ilifanya same-side sweep.",
                    self.display_name,
                )

                return False

        # ============================================================
        # SMT PASS
        # ============================================================

        smt_note = (
            "✅ SMT divergence imethibitika "
            "(primary liquidity sweep + "
            "secondary HAKUFANYA same-side sweep)."
        )

        # ============================================================
        # 5. TP / SL
        # ============================================================

        buffer = (
            price
            * (SL_BUFFER_PCT / 100)
        )

        if direction == "up":

            sl_price = (
                ob["low"]
                - buffer
            )

        else:

            sl_price = (
                ob["high"]
                + buffer
            )

        sl_distance = abs(
            price - sl_price
        )

        if sl_distance <= 0:

            return False

        if direction == "up":

            tp_price = (
                price
                + RR_RATIO
                * sl_distance
            )

        else:

            tp_price = (
                price
                - RR_RATIO
                * sl_distance
            )

        # ============================================================
        # 6. LOT SIZE
        # ============================================================

        if (
            self.point_value
            and self.point_value > 0
        ):

            risk_amount = (
                ACCOUNT_BALANCE
                * (
                    RISK_PERCENT_PER_TRADE
                    / 100
                )
            )

            lot = (
                risk_amount
                / (
                    sl_distance
                    * self.point_value
                )
            )

            lot = max(
                round(lot, 2),
                0.01,
            )

            lot_line = (
                "📊 Lot Size (pendekezo): "
                f"<b>{lot}</b>\n"
            )

        else:

            lot_line = (
                "📊 Lot Size: weka "
                "POINT_VALUES kwenye .env\n"
            )

        # ============================================================
        # 7. TRACK SIGNAL
        # ============================================================

        reserved = (
            await self.signal_tracker.reserve(
                symbol=self.primary_symbol,
                display_name=self.display_name,
                direction=direction,
                entry=price,
                tp=tp_price,
                sl=sl_price,
                signal_epoch=signal_epoch,
            )
        )

        if not reserved:

            return False

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

        # ============================================================
        # 8. SEND
        # ============================================================

        try:

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
                f"bias={self.htf.trend.upper()} + "
                f"LTF(1m) CHoCH + "
                f"Displacement + OB Retest\n"
                f"RSI(14): {rsi_val:.1f} | "
                f"Bei dhidi ya SMA{SMA_TREND}: "
                f"{'juu' if price > sma_val else 'chini'}\n"
                f"{smt_note}\n\n"
                "⭐ <b>QUALITY SIGNAL</b>\n"
                "Liquidity Sweep ✓\n"
                "Displacement ✓\n"
                "CHoCH ✓\n"
                "Valid Order Block ✓\n"
                "OB Retest ✓\n"
                "HTF Bias ✓\n"
                "RSI/SMA ✓\n"
                "SMT Divergence ✓\n\n"
                "🔓 <b>TP/SL TRACKING: ACTIVE</b>\n"
                "Signal hii itafuatiliwa hadi TP au SL, "
                "lakini haitazuia signal nyingine.\n\n"
                "⚠️ Hii ni PENDEKEZO TU "
                "(si ushauri wa kifedha)."
            )

        except Exception:

            await self.signal_tracker.remove_signal(
                signal_epoch,
                self.primary_symbol,
            )

            raise

        log.info(
            "[%s] QUALITY SIGNAL SENT | "
            "direction=%s | epoch=%s | "
            "Entry %.4f | TP %.4f | SL %.4f",
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

    client.on_candle = (
        monitor.on_candle
    )

    backoff = 5
    max_backoff = 300

    while True:

        started_at = time.time()

        try:

            await client.connect()

            # ========================================================
            # HTF HISTORY
            # ========================================================

            htf_hist = (
                await client.get_candle_history(
                    monitor.primary_symbol,
                    HTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )

            for c in htf_hist:

                monitor.htf.add_candle(
                    _to_ohlc(c)
                )

            await client.subscribe_candles(
                monitor.primary_symbol,
                HTF_GRANULARITY,
            )

            # ========================================================
            # PRIMARY LTF HISTORY
            # ========================================================

            ltf_hist = (
                await client.get_candle_history(
                    monitor.primary_symbol,
                    LTF_GRANULARITY,
                    CANDLE_COUNT,
                )
            )

            for c in ltf_hist:

                cc = _to_ohlc(c)

                monitor.ltf_closes.append(
                    cc["close"]
                )

                monitor.ltf.add_candle(
                    cc
                )

            await client.subscribe_candles(
                monitor.primary_symbol,
                LTF_GRANULARITY,
            )

            # ========================================================
            # SECONDARY HISTORY
            # ========================================================

            sec_hist = (
                await client.get_candle_history(
                    monitor.secondary_symbol,
                    LTF_GRANULARITY,
                    CANDLE_COUNT,
                )
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
                "[%s] History loaded: "
                "HTF + LTF + SMT pair.",
                monitor.display_name,
            )

            await client.wait_until_disconnected()

        except asyncio.CancelledError:

            raise

        except Exception as e:

            connected_duration = (
                time.time()
                - started_at
            )

            if connected_duration > 120:

                backoff = 5

            log.error(
                "[%s] Connection lost: %s",
                monitor.display_name,
                e,
            )

            try:

                await client.close()

            except Exception:

                pass

            await asyncio.sleep(
                backoff
            )

            backoff = min(
                backoff * 2,
                max_backoff,
            )


async def acquire_process_lock():

    """
    Inazuia bot instances mbili
    kwenye Linux host moja.

    Hii SI signal lock.
    """

    try:

        import fcntl

    except ImportError:

        log.warning(
            "fcntl haipo; process lock "
            "haijawezeshwa."
        )

        return None

    path = os.getenv(
        "SIGNAL_BOT_LOCK_FILE",
        "/tmp/deriv_signal_bot.lock",
    )

    handle = open(
        path,
        "w",
    )

    try:

        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX
            | fcntl.LOCK_NB,
        )

    except BlockingIOError:

        handle.close()

        raise RuntimeError(
            "Signal bot tayari ina-run "
            "kwenye host hii."
        )

    return handle


async def main():

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        raise SystemExit(
            "Weka TELEGRAM_BOT_TOKEN "
            "na TELEGRAM_CHAT_ID kwenye .env."
        )

    process_lock = (
        await acquire_process_lock()
    )

    try:

        telegram = TelegramNotifier(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
        )

        signal_tracker = SignalTracker()

        names = ", ".join(
            p[2]
            for p in SYMBOL_PAIRS
        )

        await telegram.send(
            "🤖 <b>Signal Bot v4 imeanza</b>\n"
            f"Symbols: {names}\n"
            f"HTF: {HTF_GRANULARITY}s | "
            f"LTF: {LTF_GRANULARITY}s\n\n"
            "⭐ <b>QUALITY MODE: ON</b>\n"
            "Liquidity Sweep ✓\n"
            "Displacement ✓\n"
            "CHoCH ✓\n"
            "Valid OB ✓\n"
            "OB Retest ✓\n"
            "HTF Bias ✓\n"
            "RSI/SMA ✓\n"
            "SMT Divergence ✓\n\n"
            "🔓 <b>GLOBAL SIGNAL LOCK: OFF</b>\n"
            "Signals nyingi zinaweza kuwa ACTIVE.\n"
            "Kila signal ina TP/SL tracking yake.\n\n"
            "🕯️ <b>CANDLE DEDUPE: ACTIVE</b>\n"
            "Ticks nyingi za candle moja "
            "hazitatengeneza signal nyingi.\n\n"
            "⚠️ Hii HAITRADE."
        )

        monitors = [
            PairMonitor(
                primary,
                secondary,
                display,
                telegram,
                signal_tracker,
            )
            for (
                primary,
                secondary,
                display,
            ) in SYMBOL_PAIRS
        ]

        await asyncio.gather(
            *(
                run_pair(m)
                for m in monitors
            )
        )

    finally:

        if process_lock is not None:

            try:

                import fcntl

                fcntl.flock(
                    process_lock.fileno(),
                    fcntl.LOCK_UN,
                )

            except Exception:

                pass

            process_lock.close()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except RuntimeError as exc:

        log.error(
            "%s",
            exc,
        )

        raise SystemExit(1)
