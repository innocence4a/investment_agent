-- 001: 初期スキーマ(Phase 1)
-- 時刻はすべて UTC の ISO8601 文字列。金額は JPY int。数量は文字列化した Decimal。

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 全判断(見送り含む)+ システムイベント(F-6 / F-11)
CREATE TABLE IF NOT EXISTS thoughts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    agent      TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- buy/sell/close/skip/risk/system
    text       TEXT NOT NULL,          -- 日本語の判断根拠・説明
    symbol     TEXT,
    confidence INTEGER,
    gate       TEXT                    -- リスクゲート結果の表示文言
);
CREATE INDEX IF NOT EXISTS idx_thoughts_ts ON thoughts(ts);

-- 約定(F-6)
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,        -- buy/sell
    qty          TEXT NOT NULL,        -- Decimal 文字列
    price        INTEGER NOT NULL,     -- 約定価格(スリッページ込み)
    notional     INTEGER NOT NULL,
    fee          INTEGER NOT NULL,
    realized_pnl INTEGER,              -- sell のみ(手数料控除後)
    decision_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

-- LLM など外部 API の全応答記録(安全ルール 5)
CREATE TABLE IF NOT EXISTS api_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- 'llm_decision' 等
    model       TEXT,
    ok          INTEGER NOT NULL,
    latency_ms  INTEGER,
    cost_usd    REAL,
    request     TEXT,
    response    TEXT
);

-- ローソク足(再起動時のチャート復元用)
CREATE TABLE IF NOT EXISTS candles (
    symbol  TEXT NOT NULL,
    tf      TEXT NOT NULL,
    open_ts TEXT NOT NULL,
    o INTEGER NOT NULL,
    h INTEGER NOT NULL,
    l INTEGER NOT NULL,
    c INTEGER NOT NULL,
    PRIMARY KEY (symbol, tf, open_ts)
);

-- 資産推移(F-10)
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts     TEXT PRIMARY KEY,
    equity INTEGER NOT NULL,
    cash   INTEGER NOT NULL
);

-- ブローカー状態(再起動復元用)
CREATE TABLE IF NOT EXISTS broker_positions (
    symbol   TEXT PRIMARY KEY,
    qty      TEXT NOT NULL,
    avg_cost TEXT NOT NULL
);
