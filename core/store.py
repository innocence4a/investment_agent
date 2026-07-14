"""記録層(F-6)。SQLite に全判断・全取引・全 API 応答・資産推移を保存する。

スキーマは core/migrations/*.sql で管理し、起動時に未適用分を順に適用する。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from core.models import Candle, EquityPoint, Fill, Position, Symbol, Thought, Timeframe

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Store:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None
        # 書き込みの直列化ロック。共有コネクション上で複数タスクの execute/commit が
        # 交錯すると、複数文の書き込み(save_broker 等)が部分コミットされ得るため、
        # すべての書き込みメソッドはこのロックの中で execute〜commit を完結させる
        self._write_lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store.open() が呼ばれていません"
        return self._db

    async def open(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"
        )
        cur = await self.db.execute("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in await cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            await self.db.executescript(path.read_text(encoding="utf-8"))
            await self.db.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)", (path.name,)
            )
        await self.db.commit()

    # ── app_state(緊急停止フラグ・月次コスト等) ──
    async def get_state(self, key: str, default: str = "") -> str:
        cur = await self.db.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.db.commit()

    async def incr_state_float(self, key: str, delta: float) -> float:
        """浮動小数の状態値をアトミックに加算する(LLM 月次コストカウンタ用)。"""
        async with self._write_lock:
            cur = await self.db.execute("SELECT value FROM app_state WHERE key = ?", (key,))
            row = await cur.fetchone()
            value = (float(row["value"]) if row else 0.0) + delta
            await self.db.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, f"{value:.6f}"),
            )
            await self.db.commit()
            return value

    # ── 思考ログ ──
    async def add_thought(self, t: Thought) -> Thought:
        async with self._write_lock:
            cur = await self.db.execute(
                "INSERT INTO thoughts (ts, agent, kind, text, symbol, confidence, gate) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t.ts.isoformat(), t.agent, t.kind, t.text, t.symbol, t.confidence, t.gate),
            )
            await self.db.commit()
            t.id = cur.lastrowid or 0
        return t

    async def recent_thoughts(self, limit: int = 50) -> list[Thought]:
        cur = await self.db.execute(
            "SELECT * FROM thoughts ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [
            Thought(
                id=r["id"], ts=datetime.fromisoformat(r["ts"]), agent=r["agent"],
                kind=r["kind"], text=r["text"], symbol=r["symbol"],
                confidence=r["confidence"], gate=r["gate"],
            )
            for r in rows
        ][::-1]

    async def last_thought_ts(self, kind: str) -> datetime | None:
        """指定 kind の最新の思考ログ時刻(相談役の日次実行判定などに使う)。"""
        cur = await self.db.execute(
            "SELECT ts FROM thoughts WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
        )
        row = await cur.fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None

    # ── 約定 ──
    async def add_fill(self, f: Fill) -> Fill:
        async with self._write_lock:
            cur = await self.db.execute(
                "INSERT INTO fills (ts, symbol, side, qty, price, notional, fee, realized_pnl,"
                " decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f.ts.isoformat(), f.symbol, f.side, str(f.qty), f.price, f.notional, f.fee,
                 f.realized_pnl, f.decision_id),
            )
            await self.db.commit()
            f.id = cur.lastrowid or 0
        return f

    async def recent_fills(self, limit: int = 50) -> list[Fill]:
        cur = await self.db.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [self._row_to_fill(r) for r in rows][::-1]

    async def fills_since(self, ts_utc_iso: str) -> list[Fill]:
        cur = await self.db.execute(
            "SELECT * FROM fills WHERE ts >= ? ORDER BY id", (ts_utc_iso,)
        )
        return [self._row_to_fill(r) for r in await cur.fetchall()]

    @staticmethod
    def _row_to_fill(r: aiosqlite.Row) -> Fill:
        return Fill(
            id=r["id"], ts=datetime.fromisoformat(r["ts"]), symbol=r["symbol"],
            side=r["side"], qty=Decimal(r["qty"]), price=r["price"], notional=r["notional"],
            fee=r["fee"], realized_pnl=r["realized_pnl"], decision_id=r["decision_id"],
        )

    async def win_loss_counts(self) -> tuple[int, int]:
        cur = await self.db.execute(
            "SELECT SUM(CASE WHEN realized_pnl >= 0 THEN 1 ELSE 0 END) AS w, "
            "SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS lo "
            "FROM fills WHERE realized_pnl IS NOT NULL"
        )
        row = await cur.fetchone()
        assert row is not None
        return int(row["w"] or 0), int(row["lo"] or 0)

    async def trade_count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM fills")
        row = await cur.fetchone()
        assert row is not None
        return int(row["n"])

    # ── API 応答記録 ──
    async def add_api_log(
        self, *, ts: datetime, kind: str, model: str | None, ok: bool,
        latency_ms: int | None, cost_usd: float | None,
        request: dict[str, Any] | None, response: dict[str, Any] | str | None,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO api_log (ts, kind, model, ok, latency_ms, cost_usd, request,"
                " response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts.isoformat(), kind, model, int(ok), latency_ms, cost_usd,
                 json.dumps(request, ensure_ascii=False) if request is not None else None,
                 json.dumps(response, ensure_ascii=False) if response is not None else None),
            )
            await self.db.commit()

    # ── ローソク足 ──
    async def upsert_candle(self, c: Candle) -> None:
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO candles (symbol, tf, open_ts, o, h, l, c)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(symbol, tf, open_ts) DO UPDATE SET"
                " o = excluded.o, h = excluded.h, l = excluded.l, c = excluded.c",
                (c.symbol, c.tf.value, c.open_ts.isoformat(), c.o, c.h, c.l, c.c),
            )
            await self.db.commit()

    async def load_candles(self, symbol: Symbol, tf: Timeframe, limit: int = 500) -> list[Candle]:
        cur = await self.db.execute(
            "SELECT * FROM candles WHERE symbol = ? AND tf = ? ORDER BY open_ts DESC LIMIT ?",
            (symbol, tf.value, limit),
        )
        rows = await cur.fetchall()
        return [
            Candle(
                symbol=r["symbol"], tf=Timeframe(r["tf"]),
                open_ts=datetime.fromisoformat(r["open_ts"]),
                o=r["o"], h=r["h"], l=r["l"], c=r["c"],
            )
            for r in rows
        ][::-1]

    # ── 資産推移 ──
    async def add_equity_snapshot(self, ts: datetime, equity: int, cash: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO equity_snapshots (ts, equity, cash) VALUES (?, ?, ?)"
                " ON CONFLICT(ts) DO UPDATE SET equity = excluded.equity, cash = excluded.cash",
                (ts.isoformat(), equity, cash),
            )
            await self.db.commit()

    async def recent_equity(self, limit: int = 500) -> list[EquityPoint]:
        cur = await self.db.execute(
            "SELECT ts, equity FROM equity_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [
            EquityPoint(ts=datetime.fromisoformat(r["ts"]), equity=r["equity"]) for r in rows
        ][::-1]

    # ── ブローカー状態(再起動復元) ──
    async def save_broker(self, cash: int, positions: dict[Symbol, Position]) -> None:
        # DELETE→INSERT は 1 トランザクションで確定させる(部分コミットするとクラッシュ時に
        # 保有ポジションの記録が消え、再起動後の損切り監視から外れるため)。
        # 書き込みロックにより他タスクの commit が割り込まないことを保証する
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO app_state (key, value) VALUES ('broker_cash', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(cash),),
            )
            await self.db.execute("DELETE FROM broker_positions")
            for p in positions.values():
                await self.db.execute(
                    "INSERT INTO broker_positions (symbol, qty, avg_cost) VALUES (?, ?, ?)",
                    (p.symbol, str(p.qty), str(p.avg_cost)),
                )
            await self.db.commit()

    async def load_broker(self) -> tuple[int | None, list[Position]]:
        cash_s = await self.get_state("broker_cash", "")
        cur = await self.db.execute("SELECT * FROM broker_positions")
        rows = await cur.fetchall()
        positions = [
            Position(symbol=r["symbol"], qty=Decimal(r["qty"]), avg_cost=Decimal(r["avg_cost"]))
            for r in rows
        ]
        return (int(cash_s) if cash_s else None), positions
