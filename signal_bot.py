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


HTF_GRANULARITY = int(
    os.getenv("SIGNAL_HTF_GRANULARITY", "900")
)

LTF_GRANULARITY = int(
    os.getenv("SIGNAL_LTF_GRANULARITY", "60")
)

CANDLE_COUNT = int(
    os.getenv("SIGNAL_CANDLE_COUNT", "200")
)


RSI_PERIOD = int(
    os.getenv("SIGNAL_RSI_PERIOD", "14")
)

RSI_OVERBOUGHT = float(
    os.getenv("SIGNAL_RSI_OVERBOUGHT", "70")
)

RSI_OVERSOLD = float(
    os.getenv("SIGNAL_RSI_OVERSOLD", "30")
)

SMA_TREND = int(
    os.getenv("SIGNAL_SMA_TREND", "50")
)


ACCOUNT_BALANCE = float(
    os.getenv("ACCOUNT_BALANCE", "10000")
)

RISK_PERCENT_PER_TRADE = float(
    os.getenv("RISK_PERCENT_PER_TRADE", "1")
)

RR_RATIO = float(
    os.getenv("RR_RATIO", "2")
)

SL_BUFFER_PCT = float(
    os.getenv("SL_BUFFER_PCT", "0.1")
)


POINT_VALUES = {}

for _pair in os.getenv(
    "POINT_VALUES",
    "R_10=1,R_25=1,R_50=1,R_75=1,R_100=1"
).split(","):

    if "=" in _pair:

        _sym, _val = _pair.split("=", 1)

        try:
            POINT_VALUES[_sym.strip()] = float(
                _val.strip()
            )

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
    }


# ============================================================
# GLOBAL SIGNAL LOCK
# ============================================================

class GlobalSignalLock:
    """
    GLOBAL signal controller.

    Bot nzima inaruhusu SIGNAL MOJA TU kuwa active.

    Lifecycle:

        No active signal
                |
                v
        Reserve signal
                |
                v
        Send signal
                |
                v
        Monitor TP / SL
                |
          +-----+-----+
          |           |
         TP          SL
          |           |
          +-----+-----+
                |
                v
        Release GLOBAL LOCK
                |
                v
        Allow new signal
    """

    def __init__(self):

        self._lock = asyncio.Lock()

        self.active = None


    async def get_active(self):

        async with self._lock:

            if self.active is None:
                return None

            return dict(self.active)


    async def reserve(
        self,
        symbol,
        display_name,
        direction,
        entry,
        tp,
        sl,
        signal_epoch=None,
    ):
        """
        Reserve the GLOBAL signal slot.

        Ikiwa signal nyingine tayari iko active,
        inarudisha False.

        Hii ndiyo sehemu muhimu inayozuia
        Volatility nyingine kutuma signal.
        """

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
                "[GLOBAL LOCK] ACTIVE -> %s %s | "
                "Entry: %.4f | TP: %.4f | SL: %.4f",
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


    async def check_and_close(
        self,
        symbol,
        candle,
    ):
        """
        Fuatilia TP/SL ya GLOBAL active signal.

        BUY:

            high >= TP  -> TP HIT
            low  <= SL  -> SL HIT

        SELL:

            low  <= TP  -> TP HIT
            high >= SL  -> SL HIT

        Signal inafuatiliwa kwenye symbol yake tu.

        Candle iliyotengeneza signal yenyewe
        HAITUMIKI kuhitisha TP/SL.
        Monitoring inaanza kwenye candle inayofuata.
        """

        async with self._lock:

            if self.active is None:
                return None


            if symbol != self.active["symbol"]:
                return None


            active = dict(self.active)


            signal_epoch = active.get(
                "signal_epoch"
            )

            candle_epoch = candle.get(
                "epoch"
            )


            # Usihesabu candle iliyotengeneza signal
            if (
                signal_epoch is not None
                and candle_epoch is not None
            ):

                try:

                    if float(candle_epoch) <= float(
                        signal_epoch
                    ):

                        return None

                except (
                    TypeError,
                    ValueError
                ):

                    pass


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
                KeyError
            ):

                return None


            direction = active["direction"]

            tp = active["tp"]

            sl = active["sl"]


            if direction == "up":

                tp_hit = high >= tp

                sl_hit = low <= sl

            else:

                tp_hit = low <= tp

                sl_hit = high >= sl


            if not tp_hit and not sl_hit:

                return None


            # TP na SL zote zimeguswa ndani ya
            # candle moja. OHLC haiwezi kutuambia
            # ni ipi iligongwa kwanza.
            if tp_hit and sl_hit:

                result = "AMBIGUOUS"

                hit_price = None

            elif tp_hit:

                result = "TP"

                hit_price = tp

            else:

                result = "SL"

                hit_price = sl


            # Fungua GLOBAL LOCK
            self.active = None


            log.info(
                "[GLOBAL LOCK] %s -> %s %s | "
                "Entry: %.4f | TP: %.4f | SL: %.4f",
                result,
                active["display_name"],
                active["direction"].upper(),
                active["entry"],
                tp,
                sl,
            )


            return {
                **active,
                "result": result,
                "hit_price": hit_price,
                "candle_epoch": candle_epoch,
            }


# ============================================================
# PAIR MONITOR
# ============================================================

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


        self.htf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf = SMCAnalyzer(
            primary_symbol
        )

        self.ltf_secondary = SMCAnalyzer(
            secondary_symbol
        )


        self.ltf_closes = deque(
            maxlen=max(
                RSI_PERIOD,
                SMA_TREND
            ) + 5
        )


        self.point_value = POINT_VALUES.get(
            primary_symbol
        )


    async def on_candle(
        self,
        symbol,
        ohlc
    ):

        try:

            granularity = int(
                ohlc.get(
                    "granularity",
                    0
                )
            )

        except (
            TypeError,
            ValueError
        ):

            return


        c = _to_ohlc(
            ohlc
        )


        # ====================================================
        # HTF
        # ====================================================

        if (
            symbol == self.primary_symbol
            and granularity == HTF_GRANULARITY
        ):

            self.htf.add_candle(
                c
            )

            return


        # ====================================================
        # PRIMARY LTF
        # ====================================================

        if (
            symbol == self.primary_symbol
            and granularity == LTF_GRANULARITY
        ):

            # ------------------------------------------------
            # STEP 1:
            # Kwanza kabisa angalia kama GLOBAL active
            # signal imefikia TP au SL.
            # ------------------------------------------------

            result = await self.signal_lock.check_and_close(
                self.primary_symbol,
                c
            )


            if result:

                await self._notify_signal_result(
                    result
                )


            # ------------------------------------------------
            # STEP 2:
            # Endelea kusasisha indicators
            # ------------------------------------------------

            self.ltf_closes.append(
                c["close"]
            )


            entry = self.ltf.add_candle(
                c
            )


            # ------------------------------------------------
            # MUHIMU:
            #
            # Kama TP/SL imegongwa kwenye candle hii,
            # usiruhusu candle hii hiyo itengeneze
            # signal mpya.
            #
            # Signal mpya itaanza kuscan kwenye candle
            # inayofuata.
            # ------------------------------------------------

            if result:

                return


            # ------------------------------------------------
            # STEP 3:
            # Tafuta signal mpya.
            #
            # _maybe_send_signal() yenyewe ina GLOBAL
            # LOCK check.
            # ------------------------------------------------

            if entry:

                await self._maybe_send_signal(
                    entry,
                    c["close"],
                    c.get("epoch"),
                )


            return


        # ====================================================
        # SECONDARY LTF
        # ====================================================

        if (
            symbol == self.secondary_symbol
            and granularity == LTF_GRANULARITY
        ):

            self.ltf_secondary.add_candle(
                c
            )


    # ========================================================
    # TP / SL NOTIFICATION
    # ========================================================

    async def _notify_signal_result(
        self,
        result
    ):

        result_type = result["result"]


        direction_text = (
            "BUY"
            if result["direction"] == "up"
            else "SELL"
        )


        if result_type == "TP":

            text = (
                "🎯 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🎯 Take Profit: "
                f"<b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                f"🔓 <b>GLOBAL LOCK: RELEASED</b>\n"
                f"Bot sasa inaruhusiwa kutafuta "
                f"signal mpya."
            )


        elif result_type == "SL":

            text = (
                "🛑 <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"🛑 Stop Loss: "
                f"<b>HIT</b> @ "
                f"{result['hit_price']:.4f}\n\n"
                f"🔓 <b>GLOBAL LOCK: RELEASED</b>\n"
                f"Bot sasa inaruhusiwa kutafuta "
                f"signal mpya."
            )


        else:

            text = (
                "⚠️ <b>TAARIFA YA SIGNAL</b>\n"
                f"Symbol: <b>{result['display_name']}</b>\n"
                f"Direction: <b>{direction_text}</b>\n"
                f"Entry: {result['entry']:.4f}\n"
                f"⚠️ TP na SL zote ziliguswa "
                f"ndani ya candle moja.\n"
                f"Haiwezekani kujua ni ipi "
                f"iligongwa kwanza kwa OHLC pekee.\n\n"
                f"🔓 <b>GLOBAL LOCK: RELEASED</b>"
            )


        try:

            await self.telegram.send(
                text
            )

        except Exception as e:

            log.error(
                "[%s] Imeshindikana kutuma "
                "taarifa ya TP/SL: %s",
                self.display_name,
                e,
            )


    # ========================================================
    # SIGNAL GENERATION
    # ========================================================

    async def _maybe_send_signal(
        self,
        entry,
        price,
        signal_epoch=None,
    ):

        # ====================================================
        # GLOBAL LOCK CHECK
        #
        # HII NDIO KANUNI KUU:
        #
        # Kama Volatility YOYOTE ina signal ACTIVE,
        # usitume signal yoyote nyingine.
        # ====================================================

        active = await self.signal_lock.get_active()


        if active is not None:

            log.info(
                "[%s] SIGNAL IMEZUIWA: "
                "GLOBAL LOCK iko ACTIVE kwa "
                "%s %s | "
                "inasubiri TP/SL.",
                self.display_name,
                active["display_name"],
                active["direction"].upper(),
            )

            return


        # ====================================================
        # ORIGINAL SIGNAL LOGIC
        # ====================================================

        direction = entry["direction"]

        ob = entry["ob"]


        if (
            self.htf.trend is None
            or self.htf.trend != direction
        ):

            log.info(
                "[%s] Ishara %s imekataliwa: "
                "haiafiki HTF(15m) bias (%s).",
                self.display_name,
                direction.upper(),
                self.htf.trend,
            )

            return


        rsi_val = rsi(
            self.ltf_closes,
            RSI_PERIOD
        )


        sma_val = sma(
            self.ltf_closes,
            SMA_TREND
        )


        if (
            rsi_val is None
            or sma_val is None
        ):

            log.info(
                "[%s] Data haitoshi bado "
                "kwa RSI/SMA confluence.",
                self.display_name,
            )

            return


        if (
            direction == "up"
            and (
                rsi_val >= RSI_OVERBOUGHT
                or price < sma_val
            )
        ):

            log.info(
                "[%s] Ishara UP imekataliwa "
                "na RSI/SMA filter.",
                self.display_name,
            )

            return


        if (
            direction == "down"
            and (
                rsi_val <= RSI_OVERSOLD
                or price > sma_val
            )
        ):

            log.info(
                "[%s] Ishara DOWN imekataliwa "
                "na RSI/SMA filter.",
                self.display_name,
            )

            return


        # ====================================================
        # SMT
        # ====================================================

        primary_swept = (
            self.ltf.last_sweep
        )

        secondary_swept = (
            self.ltf_secondary.last_sweep
        )


        if primary_swept is None:

            smt_note = (
                "ℹ️ Hakuna liquidity sweep dhahiri "
                "kabla ya CHoCH hii - "
                "SMT haikupimwa kwa ishara hii."
            )


        elif primary_swept == secondary_swept:

            smt_note = (
                "⚠️ Hakuna SMT divergence "
                "(pacha wa (1s) alifanya sweep ile ile - "
                "uthibitisho dhaifu)."
            )


        else:

            smt_note = (
                "✅ SMT divergence imethibitika "
                "(pacha wa (1s) HAKUFANYA sweep - "
                "uthibitisho mzuri)."
            )


        # ====================================================
        # SL / TP
        # ====================================================

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

            log.warning(
                "[%s] sl_distance si sahihi (0), "
                "ishara imerukwa.",
                self.display_name,
            )

            return


        tp_price = (
            price
            + RR_RATIO * sl_distance
            if direction == "up"
            else
            price
            - RR_RATIO * sl_distance
        )


        # ====================================================
        # LOT SIZE
        # ====================================================

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
                0.01
            )


            lot_line = (
                f"📊 Lot Size (pendekezo): "
                f"<b>{lot}</b>\n"
            )


        else:

            lot_line = (
                "📊 Lot Size: weka POINT_VALUES "
                "ya symbol hii kwenye .env "
                "(angalia MT5 -> Specification) "
                "kupata pendekezo\n"
            )


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


        # ====================================================
        # ATOMIC GLOBAL RESERVATION
        #
        # Hapa tunafunga nafasi ya signal KABLA ya
        # await telegram.send().
        #
        # Hii inazuia Volatility mbili kutuma signal
        # kwa wakati mmoja.
        # ====================================================

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

            active = await self.signal_lock.get_active()


            log.info(
                "[%s] Signal haijatumwa: "
                "GLOBAL LOCK imechukuliwa na "
                "%s %s.",
                self.display_name,
                (
                    active["display_name"]
                    if active
                    else "signal nyingine"
                ),
                (
                    active["direction"].upper()
                    if active
                    else ""
                ),
            )

            return


        # ====================================================
        # SEND TELEGRAM
        # ====================================================

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
                f"bias={self.htf.trend.upper()} "
                f"+ LTF(1m) CHoCH+OB retest\n"

                f"RSI(14): {rsi_val:.1f} | "
                f"Bei dhidi ya SMA{SMA_TREND}: "
                f"{'juu' if price > sma_val else 'chini'}\n"

                f"{smt_note}\n\n"

                f"🔒 <b>GLOBAL LOCK: ACTIVE</b>\n"

                f"Hakuna signal nyingine "
                f"itakayotumwa mpaka signal hii "
                f"ifike TP au SL.\n\n"

                f"⚠️ Hii ni PENDEKEZO TU "
                f"(si ushauri wa kifedha) - "
                f"fanya uamuzi wako mwenyewe "
                f"kabla ya kubonyeza kwenye MT5."

            )


        except Exception:

            # Telegram haijafanikiwa.
            # Ondoa lock ili bot isibaki locked
            # bila signal iliyotumwa.
            await self.signal_lock.release()

            raise


        # ====================================================
        # LOG
        # ====================================================

        log.info(
            "[%s] ISHARA %s IMETUMWA. "
            "Entry: %.4f | TP: %.4f | SL: %.4f | "
            "GLOBAL LOCK = ACTIVE",
            self.display_name,
            direction.upper(),
            price,
            tp_price,
            sl_price,
        )


# ============================================================
# RUN PAIR
# ============================================================

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


            log.info(
                "[%s] Umeunganishwa "
                "(public data).",
                monitor.display_name,
            )


            # =================================================
            # HTF HISTORY
            # =================================================

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


            # =================================================
            # LTF HISTORY
            # =================================================

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


            # =================================================
            # SECONDARY HISTORY
            # =================================================

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
                "[%s] Historia imepakiwa "
                "(HTF+LTF+pacha), "
                "inasubiri candles mpya...",
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
                "[%s] Muunganiko umekatika: %s",
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
                max_backoff
            )


# ============================================================
# MAIN
# ============================================================

async def main():

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        raise SystemExit(
            "Weka TELEGRAM_BOT_TOKEN "
            "na TELEGRAM_CHAT_ID kwenye .env."
        )


    telegram = TelegramNotifier(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )


    # ========================================================
    # ONE GLOBAL LOCK FOR THE ENTIRE BOT
    #
    # Hii object moja inatumika na:
    #
    # Volatility 10
    # Volatility 25
    # Volatility 50
    # Volatility 75
    # Volatility 100
    #
    # Kwa hiyo signal moja tu inaweza kuwa ACTIVE.
    # ========================================================

    signal_lock = GlobalSignalLock()


    names = ", ".join(
        p[2]
        for p in SYMBOL_PAIRS
    )


    await telegram.send(

        f"🤖 <b>Signal Bot v2 imeanza "
        f"(SMC/SMT + HTF/LTF + RSI/SMA)</b>\n"

        f"Symbols: {names}\n"

        f"HTF bias: "
        f"{HTF_GRANULARITY}s | "

        f"LTF entry: "
        f"{LTF_GRANULARITY}s\n\n"

        f"🔒 <b>GLOBAL SIGNAL LOCK: READY</b>\n"

        f"Signal moja tu inaweza kuwa "
        f"ACTIVE kwa wakati mmoja.\n"

        f"Signal mpya itaruhusiwa baada ya "
        f"TP au SL ya signal iliyopo "
        f"kugunduliwa.\n\n"

        f"⚠️ Hii HAITRADE - "
        f"inatuma mapendekezo "
        f"(Entry/TP/SL/Lot) TU."

    )


    # ========================================================
    # CREATE ALL MONITORS
    #
    # WOTE wanatumia signal_lock HIYO HIYO.
    # ========================================================

    monitors = [

        PairMonitor(
            primary,
            secondary,
            display,
            telegram,
            signal_lock,
        )

        for primary, secondary, display
        in SYMBOL_PAIRS

    ]


    try:

        await asyncio.gather(

            *(
                run_pair(m)
                for m in monitors
            )

        )


    except KeyboardInterrupt:

        await telegram.send(
            "🛑 Signal bot imesimamishwa "
            "na mtumiaji."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
)
