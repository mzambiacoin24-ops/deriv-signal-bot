def sma(values, period):
    """
    Simple Moving Average (SMA).

    values: list/deque ya closing prices
    period: idadi ya candles
    """
    if values is None:
        return None

    if period <= 0:
        return None

    if len(values) < period:
        return None

    data = list(values)[-period:]

    try:
        return sum(float(x) for x in data) / period
    except (TypeError, ValueError):
        return None


def rsi(values, period=14):
    """
    Relative Strength Index (RSI).

    Uses the standard simple average method
    for the initial RSI calculation.
    """

    if values is None:
        return None

    if period <= 0:
        return None

    data = list(values)

    # RSI inahitaji angalau period + 1 closing prices
    if len(data) < period + 1:
        return None

    try:
        closes = [float(x) for x in data]
    except (TypeError, ValueError):
        return None

    recent = closes[-(period + 1):]

    gains = []
    losses = []

    for i in range(1, len(recent)):
        change = recent[i] - recent[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        if average_gain == 0:
            return 50.0

        return 100.0

    rs = average_gain / average_loss

    value = 100.0 - (100.0 / (1.0 + rs))

    return value
