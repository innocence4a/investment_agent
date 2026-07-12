"""テクニカル指標(LLM 判断のインプット用)。

ダッシュボード側の描画用計算は dashboard/src/indicators.ts が独立に行う。
ここでの計算は判断材料であり金銭計算ではないため float を用いる。
"""

from __future__ import annotations


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float]:
    out: list[float] = []
    k = 2 / (n + 1)
    e: float | None = None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    ag = 0.0
    al = 0.0
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        g, loss = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            ag += g
            al += loss
            if i == n:
                ag /= n
                al /= n
                out[i] = 100 - 100 / (1 + (100.0 if al == 0 else ag / al))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + loss) / n
            out[i] = 100 - 100 / (1 + (100.0 if al == 0 else ag / al))
    return out


def macd(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    """(MACD ライン, シグナル, ヒストグラム) を返す。"""
    e12, e26 = ema(values, 12), ema(values, 26)
    line = [a - b for a, b in zip(e12, e26, strict=True)]
    signal = ema(line, 9)
    hist = [a - b for a, b in zip(line, signal, strict=True)]
    return line, signal, hist
