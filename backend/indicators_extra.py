"""额外指标：UT Bot Alerts / STC / Hull Suite（供 S16 使用，纯 Python 无依赖）"""


def _ema(vals, length):
    n = len(vals); out = [None]*n; k = 2.0/(length+1); acc = None
    for i, v in enumerate(vals):
        if v is None:
            continue
        acc = v if acc is None else v*k + acc*(1-k)
        out[i] = acc
    return out


def _wma(vals, length):
    length = int(length)
    n = len(vals); out = [None]*n
    if length < 1:
        return out
    denom = length*(length+1)/2.0
    for i in range(length-1, n):
        w = vals[i-length+1:i+1]
        if any(x is None for x in w):
            continue
        s = 0.0
        for j, v in enumerate(w):
            s += v*(j+1)
        out[i] = s/denom
    return out


def _rma(vals, length):
    n = len(vals); out = [None]*n; acc = None
    for i, v in enumerate(vals):
        if v is None:
            continue
        if acc is None:
            if i >= length-1:
                w = vals[i-length+1:i+1]
                if any(x is None for x in w):
                    continue
                acc = sum(w)/length
                out[i] = acc
        else:
            acc = (acc*(length-1)+v)/length
            out[i] = acc
    return out


def _tr(high, low, close):
    n = len(close); out = [None]*n
    for i in range(1, n):
        out[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    return out


def ut_bot(close, high, low, key_value=2.0, atr_period=6):
    """UT Bot Alerts (QuantNomad)：ATR 追踪止损 + 穿越信号"""
    n = len(close)
    atr = _rma(_tr(high, low, close), atr_period)
    trail = [None]*n
    buy = [False]*n
    sell = [False]*n
    for i in range(1, n):
        if atr[i] is None:
            continue
        nloss = key_value * atr[i]
        prev = trail[i-1] if trail[i-1] is not None else 0.0
        s, s1 = close[i], close[i-1]
        if s > prev and s1 > prev:
            t = max(prev, s - nloss)
        elif s < prev and s1 < prev:
            t = min(prev, s + nloss)
        elif s > prev:
            t = s - nloss
        else:
            t = s + nloss
        trail[i] = t
        if s > t and s1 <= prev:
            buy[i] = True
        if s < t and s1 >= prev:
            sell[i] = True
    return buy, sell, trail


def stc(close, fast=27, slow=80, cycle=10):
    """Schaff Trend Cycle（两次随机 + 0.5 平滑）"""
    n = len(close)
    ef, es = _ema(close, fast), _ema(close, slow)
    macd = [None if (a is None or b is None) else a-b for a, b in zip(ef, es)]

    def stoch_of(series):
        out = [None]*n
        for i in range(cycle-1, n):
            w = series[i-cycle+1:i+1]
            if any(x is None for x in w):
                continue
            lo, hi = min(w), max(w)
            out[i] = 0.0 if hi == lo else (series[i]-lo)/(hi-lo)*100.0
        return out

    k1 = stoch_of(macd)
    f1 = [None]*n; acc = None
    for i in range(n):
        if k1[i] is None:
            continue
        acc = k1[i] if acc is None else acc + 0.5*(k1[i]-acc)
        f1[i] = acc
    k2 = stoch_of(f1)
    out = [None]*n; acc = None
    for i in range(n):
        if k2[i] is None:
            continue
        acc = k2[i] if acc is None else acc + 0.5*(k2[i]-acc)
        out[i] = acc
    return out


def hull_hma(close, length=55):
    """Hull Suite (InSilico) - HMA 模式"""
    import math
    length = int(length)
    half = max(1, int(length/2))
    root = max(1, int(round(math.sqrt(length))))
    w1 = _wma(close, half)
    w2 = _wma(close, length)
    diff = [None if (a is None or b is None) else 2*a-b for a, b in zip(w1, w2)]
    return _wma(diff, root)
