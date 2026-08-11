# symbol_specs.py
# Deriv Symbol Specifications
# Public market-data connection only.
# Haitumii MT5 token.

import asyncio
import json
import math
import websockets


DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"


async def get_symbol_specs(symbol: str):
    """
    Pata specifications za symbol kutoka Deriv.

    Inarudisha:
        symbol
        display_name
        pip_size
        decimals
        min_tick
        raw_active_symbol
        raw_contracts
    """

    async with websockets.connect(
        DERIV_WS_URL,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10
    ) as ws:

        # ---------------------------------------------------------
        # 1. ACTIVE SYMBOL
        # ---------------------------------------------------------
        await ws.send(json.dumps({
            "active_symbols": "brief",
            "product_type": "basic"
        }))

        response = json.loads(await ws.recv())

        if "error" in response:
            raise RuntimeError(
                f"Deriv active_symbols error: {response['error']}"
            )

        symbols = response.get("active_symbols", [])

        selected = None

        for item in symbols:
            if item.get("symbol") == symbol:
                selected = item
                break

        if selected is None:
            raise ValueError(
                f"Symbol haikupatikana Deriv: {symbol}"
            )

        # ---------------------------------------------------------
        # 2. PIP SIZE / DECIMALS
        # ---------------------------------------------------------
        pip_size = selected.get("pip")

        if pip_size is None:
            # fallback
            pip_size = 1.0

        try:
            pip_size = float(pip_size)
        except (TypeError, ValueError):
            pip_size = 1.0

        decimals = get_decimal_places(pip_size)

        # ---------------------------------------------------------
        # 3. CONTRACT SPECIFICATIONS
        # ---------------------------------------------------------
        await ws.send(json.dumps({
            "contracts_for": symbol
        }))

        contract_response = json.loads(await ws.recv())

        if "error" in contract_response:
            # Contracts_for inaweza kutokuwepo kwa baadhi ya symbols.
            contract_data = {}
        else:
            contract_data = contract_response.get(
                "contracts_for",
                {}
            )

        return {
            "symbol": symbol,
            "display_name": selected.get(
                "display_name",
                symbol
            ),
            "pip_size": pip_size,
            "decimals": decimals,
            "min_tick": pip_size,
            "raw_active_symbol": selected,
            "raw_contracts": contract_data
        }


def get_decimal_places(value: float) -> int:
    """
    Mfano:
        1       -> 0
        0.1     -> 1
        0.01    -> 2
        0.001   -> 3
    """

    if value <= 0:
        return 0

    text = f"{value:.12f}".rstrip("0")

    if "." not in text:
        return 0

    return len(text.split(".")[1])


def normalize_price(price: float, specs: dict) -> float:
    """
    Rekebisha price kulingana na decimal/pip size ya symbol.
    """

    pip = float(specs["pip_size"])

    if pip <= 0:
        return price

    # Round kwenye tick size
    normalized = round(
        round(price / pip) * pip,
        specs["decimals"]
    )

    return normalized


def price_distance_in_ticks(
    price_a: float,
    price_b: float,
    specs: dict
) -> int:
    """
    Hesabu tofauti ya bei kwa ticks/pips.
    """

    pip = float(specs["pip_size"])

    if pip <= 0:
        return 0

    return int(
        round(abs(price_a - price_b) / pip)
    )


def validate_tp_sl(
    direction: str,
    entry: float,
    take_profit: float,
    stop_loss: float,
    specs: dict
):
    """
    Hakikisha TP/SL ziko upande sahihi wa trade
    na zime-align na tick size ya symbol.

    HAIJARIBU MT5 broker stop-level.
    Hiyo tutaiunganisha baadaye kupitia MT5 bridge.
    """

    direction = direction.upper()

    entry = normalize_price(entry, specs)
    take_profit = normalize_price(take_profit, specs)
    stop_loss = normalize_price(stop_loss, specs)

    errors = []

    if direction == "BUY":

        if take_profit <= entry:
            errors.append(
                "BUY TP lazima iwe juu ya entry."
            )

        if stop_loss >= entry:
            errors.append(
                "BUY SL lazima iwe chini ya entry."
            )

    elif direction == "SELL":

        if take_profit >= entry:
            errors.append(
                "SELL TP lazima iwe chini ya entry."
            )

        if stop_loss <= entry:
            errors.append(
                "SELL SL lazima iwe juu ya entry."
            )

    else:
        errors.append(
            f"Direction
