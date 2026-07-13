import type {
  Candle, Config, EquityPoint, Fill, Kpi, Position, ServerMessage, Snapshot, Symbol_, Thought,
  Timeframe,
} from "./types";

const MAX_CANDLES = 500;
const MAX_EQUITY = 600;
const MAX_THOUGHTS = 60;
const MAX_FILLS = 60;

export class AppState {
  config: Config | null = null;
  kpi: Kpi | null = null;
  halted = false;
  connected = false;
  prices: Partial<Record<Symbol_, number>> = {};
  candles: Record<string, Candle[]> = {}; // key: `${symbol}:${tf}`
  equity: EquityPoint[] = [];
  thoughts: Thought[] = []; // 新しい順
  fills: Fill[] = []; // 新しい順
  positions: Position[] = [];
  activeSym: Symbol_ = "BTC_JPY";
  activeTf: Timeframe = "5m";

  series(sym: Symbol_, tf: Timeframe): Candle[] {
    return this.candles[`${sym}:${tf}`] ?? [];
  }

  applySnapshot(s: Snapshot): void {
    this.config = s.config;
    this.kpi = s.kpi;
    this.halted = s.halted;
    this.prices = s.prices;
    this.candles = {};
    for (const [sym, byTf] of Object.entries(s.candles)) {
      for (const [tf, arr] of Object.entries(byTf)) {
        this.candles[`${sym}:${tf}`] = arr;
      }
    }
    this.equity = s.equity;
    this.thoughts = [...s.thoughts].reverse();
    this.fills = [...s.fills].reverse();
    this.positions = s.positions;
  }

  mergeCandle(c: Candle): void {
    const key = `${c.symbol}:${c.tf}`;
    const arr = this.candles[key] ?? (this.candles[key] = []);
    const last = arr[arr.length - 1];
    if (last && last.open_ts === c.open_ts) {
      arr[arr.length - 1] = c;
    } else if (!last || c.open_ts > last.open_ts) {
      arr.push(c);
      if (arr.length > MAX_CANDLES) arr.shift();
    }
  }

  /** メッセージを反映し、更新された領域を返す(描画の間引き用) */
  apply(msg: ServerMessage): void {
    switch (msg.type) {
      case "snapshot":
        this.applySnapshot(msg.data);
        break;
      case "tick":
        this.prices[msg.data.symbol] = msg.data.price;
        for (const c of msg.data.candles) this.mergeCandle(c);
        break;
      case "thought":
        this.thoughts.unshift(msg.data);
        this.thoughts.length = Math.min(this.thoughts.length, MAX_THOUGHTS);
        break;
      case "fill":
        this.fills.unshift(msg.data);
        this.fills.length = Math.min(this.fills.length, MAX_FILLS);
        break;
      case "positions":
        this.positions = msg.data;
        break;
      case "kpi":
        this.kpi = msg.data;
        this.halted = msg.data.halted;
        break;
      case "equity":
        this.equity.push(msg.data);
        if (this.equity.length > MAX_EQUITY) this.equity.shift();
        break;
      case "halt":
        this.halted = msg.data.halted;
        break;
    }
  }
}
