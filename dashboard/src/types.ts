// コア(Python 側 core/models.py)の WS メッセージと対になる型定義

export type Symbol_ = "BTC_JPY" | "ETH_JPY";
export type Timeframe = "5m" | "1h" | "4h" | "1d" | "1w" | "1M";

export const TIMEFRAMES: { key: Timeframe; label: string; mins: number }[] = [
  { key: "5m", label: "5分", mins: 5 },
  { key: "1h", label: "1時間", mins: 60 },
  { key: "4h", label: "4時間", mins: 240 },
  { key: "1d", label: "日足", mins: 1440 },
  { key: "1w", label: "週足", mins: 10080 },
  { key: "1M", label: "月足", mins: 43200 },
];

export interface Candle {
  symbol: Symbol_;
  tf: Timeframe;
  open_ts: string; // ISO8601 UTC
  o: number;
  h: number;
  l: number;
  c: number;
}

export type ThoughtKind = "buy" | "sell" | "close" | "skip" | "risk" | "system";

export interface Thought {
  id: number;
  ts: string;
  agent: string;
  kind: ThoughtKind;
  text: string;
  symbol: Symbol_ | null;
  confidence: number | null;
  gate: string | null;
}

export interface Fill {
  id: number;
  ts: string;
  symbol: Symbol_;
  side: "buy" | "sell";
  qty: string; // Decimal 文字列
  price: number;
  notional: number;
  fee: number;
  realized_pnl: number | null;
}

export interface Position {
  symbol: Symbol_;
  qty: string;
  avg_cost: number;
}

export interface Kpi {
  equity: number;
  cash: number;
  start_capital: number;
  pnl_total: number;
  pnl_today: number;
  wins: number;
  losses: number;
  trades: number;
  started_at: string;
  halted: boolean;
  monthly_llm_cost_usd: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
}

export interface Config {
  mode: string;
  feed: string;
  llm: string;
  decision_interval_sec: number;
  symbols: Symbol_[];
}

export interface Snapshot {
  config: Config;
  kpi: Kpi;
  halted: boolean;
  prices: Partial<Record<Symbol_, number>>;
  candles: Record<Symbol_, Record<Timeframe, Candle[]>>;
  equity: EquityPoint[];
  thoughts: Thought[];
  fills: Fill[];
  positions: Position[];
}

export type ServerMessage =
  | { type: "snapshot"; data: Snapshot }
  | { type: "tick"; data: { symbol: Symbol_; price: number; ts: string; candles: Candle[] } }
  | { type: "thought"; data: Thought }
  | { type: "fill"; data: Fill }
  | { type: "positions"; data: Position[] }
  | { type: "kpi"; data: Kpi }
  | { type: "equity"; data: EquityPoint }
  | { type: "halt"; data: { halted: boolean } };
