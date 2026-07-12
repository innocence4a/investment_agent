// チャート描画用のテクニカル指標(モックと同一ロジック)

export function sma(vals: number[], n: number): (number | null)[] {
  const out: (number | null)[] = Array(vals.length).fill(null);
  let s = 0;
  for (let i = 0; i < vals.length; i++) {
    s += vals[i]!;
    if (i >= n) s -= vals[i - n]!;
    if (i >= n - 1) out[i] = s / n;
  }
  return out;
}

export function ema(vals: number[], n: number): number[] {
  const out: number[] = [];
  const k = 2 / (n + 1);
  let e: number | null = null;
  for (const v of vals) {
    e = e === null ? v : v * k + e * (1 - k);
    out.push(e);
  }
  return out;
}

export function rsi(vals: number[], n = 14): (number | null)[] {
  const out: (number | null)[] = Array(vals.length).fill(null);
  let ag = 0;
  let al = 0;
  for (let i = 1; i < vals.length; i++) {
    const d = vals[i]! - vals[i - 1]!;
    const g = Math.max(d, 0);
    const l = Math.max(-d, 0);
    if (i <= n) {
      ag += g;
      al += l;
      if (i === n) {
        ag /= n;
        al /= n;
        out[i] = 100 - 100 / (1 + (al === 0 ? 100 : ag / al));
      }
    } else {
      ag = (ag * (n - 1) + g) / n;
      al = (al * (n - 1) + l) / n;
      out[i] = 100 - 100 / (1 + (al === 0 ? 100 : ag / al));
    }
  }
  return out;
}

export function macd(vals: number[]): { line: number[]; signal: number[]; hist: number[] } {
  const e12 = ema(vals, 12);
  const e26 = ema(vals, 26);
  const line = vals.map((_, i) => e12[i]! - e26[i]!);
  const signal = ema(line, 9);
  return { line, signal, hist: line.map((v, i) => v - signal[i]!) };
}
