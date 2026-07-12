// Canvas 自前描画(外部チャートライブラリ禁止 — モックの見た目を正とする)
// 描画ロジックは docs/dashboard-mockup.html から移植し、実データ(AppState)に接続

import { macd, rsi, sma } from "./indicators";
import type { AppState } from "./state";
import { tfTimeLabel, yen } from "./format";
import { TIMEFRAMES } from "./types";
import type { Candle } from "./types";

const VIEW = 24; // X 軸の基準線 24 本 = 表示幅 24 本足(要件 F-9)

const css = (n: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(n).trim();

interface Colors {
  up: string; down: string; upT: string; downT: string;
  accent: string; grid: string; muted: string; ink2: string;
}

const MAS = [
  { n: 7, color: "#d7a94e" },
  { n: 25, color: "#5c8ee8" },
  { n: 99, color: "#9b87e8" },
] as const;

interface FitResult { w: number; h: number }

/** HiDPI 対応リサイズ。論理高さは初回に固定
 *(DPR 反映後の height 属性を再読込すると倍々に肥大するバグの再発防止)*/
function fit(cv: HTMLCanvasElement & { _h?: number }): FitResult {
  if (!cv._h) cv._h = Number(cv.getAttribute("height"));
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth;
  const h = cv._h;
  const bw = Math.round(w * dpr);
  const bh = Math.round(h * dpr);
  if (cv.width !== bw || cv.height !== bh) {
    cv.width = bw;
    cv.height = bh;
    cv.style.height = h + "px";
  }
  cv.getContext("2d")!.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}

interface Geom { padL: number; padR: number; bw: number; view: Candle[] }

export class Charts {
  private colors: Colors;
  private pc: HTMLCanvasElement;
  private rc: HTMLCanvasElement;
  private mc: HTMLCanvasElement;
  private ec: HTMLCanvasElement;
  private tip: HTMLElement;
  private hoverIdx = -1;
  private lastMouse: { x: number; y: number } | null = null;
  private geom: Geom | null = null;

  constructor(private state: AppState) {
    this.colors = {
      up: css("--up"), down: css("--down"), upT: css("--up-text"), downT: css("--down-text"),
      accent: css("--accent"), grid: css("--line"), muted: css("--muted"), ink2: css("--ink2"),
    };
    this.pc = document.getElementById("priceChart") as HTMLCanvasElement;
    this.rc = document.getElementById("rsiChart") as HTMLCanvasElement;
    this.mc = document.getElementById("macdChart") as HTMLCanvasElement;
    this.ec = document.getElementById("equityChart") as HTMLCanvasElement;
    this.tip = document.getElementById("tip")!;
    this.pc.addEventListener("mousemove", (e) => {
      const r = this.pc.getBoundingClientRect();
      this.lastMouse = { x: e.clientX - r.left, y: e.clientY - r.top };
      this.resolveHover();
    });
    this.pc.addEventListener("mouseleave", () => {
      this.lastMouse = null;
      this.tip.style.display = "none";
      this.hoverIdx = -1;
      this.drawPrice();
    });
  }

  drawAll(): void {
    this.drawPrice();
    this.drawIndicators();
    this.drawEquity();
    if (this.lastMouse) this.resolveHover();
  }

  private series(): Candle[] {
    return this.state.series(this.state.activeSym, this.state.activeTf);
  }

  // ── 価格チャート(ローソク足 + MA + 売買マーカー)──
  drawPrice(): void {
    const { w, h } = fit(this.pc);
    const ctx = this.pc.getContext("2d")!;
    const C = this.colors;
    const s = this.series();
    ctx.clearRect(0, 0, w, h);
    if (s.length === 0) {
      ctx.fillStyle = C.muted;
      ctx.font = "12px " + css("--sans");
      ctx.textAlign = "center";
      ctx.fillText("データ受信待ち…", w / 2, h / 2);
      return;
    }
    const view = s.slice(-VIEW);
    const pad = { l: 8, r: 86, t: 10, b: 24 };
    const closes = s.map((k) => k.c);
    const maLines = MAS.map((ma) => sma(closes, ma.n).slice(-view.length));
    const maVals = maLines.flat().filter((v): v is number => v !== null);
    const lo = Math.min(...view.map((c) => c.l), ...(maVals.length ? maVals : [Infinity]));
    const hi = Math.max(...view.map((c) => c.h), ...(maVals.length ? maVals : [-Infinity]));
    const range = Math.max(hi - lo, 1);
    const y = (v: number): number => pad.t + ((hi - v) / range) * (h - pad.t - pad.b);
    const bw = (w - pad.l - pad.r) / VIEW;

    // グリッド(横 4 本)+ 右軸ラベル
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = C.muted;
    ctx.font = "11px ui-monospace,Menlo,monospace";
    ctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) {
      const v = hi - (range * i) / 4;
      const gy = y(v);
      ctx.beginPath();
      ctx.moveTo(pad.l, gy);
      ctx.lineTo(w - pad.r, gy);
      ctx.stroke();
      ctx.fillText(yen(v), w - pad.r + 8, gy + 4);
    }
    // X 軸: 基準線 24 本 + 時刻ラベル(4 本ごと・JST)
    const tfMins = TIMEFRAMES.find((t) => t.key === this.state.activeTf)?.mins ?? 5;
    const base = s.length - view.length;
    ctx.textAlign = "center";
    for (let i = 0; i < VIEW; i++) {
      const x = pad.l + i * bw + bw / 2;
      ctx.strokeStyle = C.grid;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, h - pad.b);
      ctx.stroke();
      ctx.globalAlpha = 1;
      const c = view[i];
      if (c && (base + i) % 4 === 0) {
        ctx.fillStyle = C.muted;
        ctx.fillText(tfTimeLabel(c.open_ts, tfMins), x, h - 8);
      }
    }
    // 移動平均線 3 本(MA7/25/99)
    MAS.forEach((ma, mi) => {
      const line = maLines[mi]!;
      ctx.strokeStyle = ma.color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let started = false;
      line.forEach((v, i) => {
        if (v === null) return;
        const x = pad.l + i * bw + bw / 2;
        if (!started) {
          ctx.moveTo(x, y(v));
          started = true;
        } else ctx.lineTo(x, y(v));
      });
      ctx.stroke();
    });
    // MA 凡例
    ctx.textAlign = "left";
    ctx.font = "10.5px ui-monospace,Menlo,monospace";
    let lx = pad.l + 4;
    for (const ma of MAS) {
      ctx.fillStyle = ma.color;
      ctx.fillRect(lx, pad.t + 2, 8, 2.5);
      ctx.fillStyle = C.ink2;
      ctx.fillText(`MA${ma.n}`, lx + 11, pad.t + 7);
      lx += 52;
    }
    // ローソク
    view.forEach((c, i) => {
      const x = pad.l + i * bw + bw / 2;
      const up = c.c >= c.o;
      ctx.strokeStyle = ctx.fillStyle = up ? C.up : C.down;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, y(c.h));
      ctx.lineTo(x, y(c.l));
      ctx.stroke();
      const bh = Math.max(1.5, Math.abs(y(c.o) - y(c.c)));
      ctx.fillRect(x - bw * 0.32, Math.min(y(c.o), y(c.c)), bw * 0.64, bh);
    });
    // 売買マーカー(▲▼ — 色に加えて記号で方向を二重符号化)。5分足のみ表示
    if (this.state.activeTf === "5m") {
      const openEpochs = view.map((c) => Date.parse(c.open_ts));
      for (const f of this.state.fills) {
        if (f.symbol !== this.state.activeSym) continue;
        const bucket = Math.floor(Date.parse(f.ts) / 300_000) * 300_000;
        const i = openEpochs.indexOf(bucket);
        if (i < 0) continue;
        const c = view[i]!;
        const x = pad.l + i * bw + bw / 2;
        ctx.font = "bold 11px ui-monospace,Menlo,monospace";
        ctx.textAlign = "center";
        if (f.side === "buy") {
          ctx.fillStyle = C.upT;
          ctx.fillText("▲", x, y(c.l) + 14);
        } else {
          ctx.fillStyle = C.downT;
          ctx.fillText("▼", x, y(c.h) - 6);
        }
      }
    }
    // 現在値ライン + タグ
    const last = view[view.length - 1]!;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = C.ink2;
    ctx.beginPath();
    ctx.moveTo(pad.l, y(last.c));
    ctx.lineTo(w - pad.r, y(last.c));
    ctx.stroke();
    ctx.setLineDash([]);
    const tagY = y(last.c);
    ctx.fillStyle = last.c >= last.o ? C.up : C.down;
    ctx.beginPath();
    ctx.roundRect(w - pad.r + 2, tagY - 10, pad.r - 6, 20, 5);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.textAlign = "left";
    ctx.fillText(yen(last.c), w - pad.r + 8, tagY + 4);
    // ホバー十字線
    if (this.hoverIdx >= 0 && this.hoverIdx < view.length) {
      const x = pad.l + this.hoverIdx * bw + bw / 2;
      ctx.strokeStyle = C.muted;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, h - pad.b);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    this.geom = { padL: pad.l, padR: pad.r, bw, view };
  }

  // ── RSI / MACD サブチャート ──
  drawIndicators(): void {
    const C = this.colors;
    const closes = this.series().map((k) => k.c);
    {
      const { w, h } = fit(this.rc);
      const ctx = this.rc.getContext("2d")!;
      const pad = { l: 8, r: 86, t: 6, b: 6 };
      const bw = (w - pad.l - pad.r) / VIEW;
      const vals = rsi(closes).slice(-VIEW);
      const y = (v: number): number => pad.t + ((100 - v) / 100) * (h - pad.t - pad.b);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "rgba(92,142,232,.06)";
      ctx.fillRect(pad.l, y(70), w - pad.l - pad.r, y(30) - y(70));
      ctx.strokeStyle = C.grid;
      ctx.setLineDash([3, 3]);
      for (const v of [30, 70]) {
        ctx.beginPath();
        ctx.moveTo(pad.l, y(v));
        ctx.lineTo(w - pad.r, y(v));
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.strokeStyle = "#d7a94e";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let started = false;
      vals.forEach((v, i) => {
        if (v === null) return;
        const x = pad.l + i * bw + bw / 2;
        if (!started) {
          ctx.moveTo(x, y(v));
          started = true;
        } else ctx.lineTo(x, y(v));
      });
      ctx.stroke();
      ctx.font = "10.5px ui-monospace,Menlo,monospace";
      ctx.textAlign = "left";
      ctx.fillStyle = C.ink2;
      ctx.fillText("RSI(14)", pad.l + 4, pad.t + 9);
      const cur = vals[vals.length - 1];
      if (cur !== null && cur !== undefined) {
        ctx.fillStyle = cur >= 70 ? C.downT : cur <= 30 ? C.upT : C.ink2;
        ctx.fillText(cur.toFixed(1), w - pad.r + 8, y(cur) + 4);
      }
    }
    {
      const { w, h } = fit(this.mc);
      const ctx = this.mc.getContext("2d")!;
      const pad = { l: 8, r: 86, t: 6, b: 6 };
      const bw = (w - pad.l - pad.r) / VIEW;
      const m = macd(closes);
      const line = m.line.slice(-VIEW);
      const sig = m.signal.slice(-VIEW);
      const hist = m.hist.slice(-VIEW);
      const ex = Math.max(...[...line, ...sig, ...hist].map(Math.abs), 1);
      const y = (v: number): number => pad.t + ((ex - v) / (2 * ex)) * (h - pad.t - pad.b);
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = C.grid;
      ctx.beginPath();
      ctx.moveTo(pad.l, y(0));
      ctx.lineTo(w - pad.r, y(0));
      ctx.stroke();
      hist.forEach((v, i) => {
        const x = pad.l + i * bw + bw / 2;
        const y0 = y(0);
        const y1 = y(v);
        ctx.fillStyle = v >= 0 ? "rgba(42,168,118,.5)" : "rgba(224,85,96,.5)";
        ctx.fillRect(x - bw * 0.28, Math.min(y0, y1), bw * 0.56, Math.max(1, Math.abs(y1 - y0)));
      });
      const pairs: [number[], string][] = [[line, C.accent], [sig, "#d7a94e"]];
      for (const [vals, col] of pairs) {
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        let started = false;
        vals.forEach((v, i) => {
          const x = pad.l + i * bw + bw / 2;
          if (!started) {
            ctx.moveTo(x, y(v));
            started = true;
          } else ctx.lineTo(x, y(v));
        });
        ctx.stroke();
      }
      ctx.font = "10.5px ui-monospace,Menlo,monospace";
      ctx.textAlign = "left";
      ctx.fillStyle = C.ink2;
      ctx.fillText("MACD(12,26,9)", pad.l + 4, pad.t + 9);
    }
  }

  // ── 資産推移(エクイティカーブ)──
  drawEquity(): void {
    const { w, h } = fit(this.ec);
    const ctx = this.ec.getContext("2d")!;
    const C = this.colors;
    const START = this.state.kpi?.start_capital ?? 1_000_000;
    const view = this.state.equity.slice(-160).map((p) => p.equity);
    ctx.clearRect(0, 0, w, h);
    if (view.length === 0) view.push(START);
    if (view.length === 1) view.push(view[0]!);
    const pad = { l: 8, r: 86, t: 8, b: 8 };
    const lo = Math.min(...view, START) - 400;
    const hi = Math.max(...view, START) + 400;
    const y = (v: number): number => pad.t + ((hi - v) / (hi - lo)) * (h - pad.t - pad.b);
    const x = (i: number): number => pad.l + (i / (view.length - 1)) * (w - pad.l - pad.r);
    // 開始ライン
    ctx.strokeStyle = C.grid;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.l, y(START));
    ctx.lineTo(w - pad.r, y(START));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "11px ui-monospace,Menlo,monospace";
    // エリア + ライン
    const grad = ctx.createLinearGradient(0, pad.t, 0, h);
    grad.addColorStop(0, "rgba(92,142,232,.28)");
    grad.addColorStop(1, "rgba(92,142,232,0)");
    ctx.beginPath();
    ctx.moveTo(x(0), h - pad.b);
    view.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.lineTo(x(view.length - 1), h - pad.b);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    view.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 2;
    ctx.stroke();
    // 終端ドット + 現在値
    const lv = view[view.length - 1]!;
    ctx.fillStyle = C.accent;
    ctx.beginPath();
    ctx.arc(x(view.length - 1), y(lv), 3.5, 0, 7);
    ctx.fill();
    if (Math.abs(y(lv) - y(START)) >= 14) {
      ctx.fillStyle = C.muted;
      ctx.fillText(yen(START), w - pad.r + 8, y(START) + 4);
    }
    ctx.fillStyle = lv >= START ? C.upT : C.downT;
    ctx.fillText(yen(lv), w - pad.r + 8, y(lv) + 4);
  }

  // ── ホバーツールチップ ──
  private resolveHover(): void {
    const g = this.geom;
    const m = this.lastMouse;
    if (!g || !m) return;
    const r = this.pc.getBoundingClientRect();
    this.hoverIdx = Math.floor((m.x - g.padL) / g.bw);
    const c = g.view[this.hoverIdx];
    if (!c || m.x > r.width - g.padR) {
      this.tip.style.display = "none";
      this.hoverIdx = -1;
      this.drawPrice();
      return;
    }
    this.tip.style.display = "block";
    this.tip.style.left = Math.min(m.x + 14, r.width - 190) + "px";
    this.tip.style.top = m.y + 14 + "px";
    this.tip.innerHTML =
      `始値 <span class="num">${yen(c.o)}</span><br>高値 <span class="num">${yen(c.h)}</span>` +
      `<br>安値 <span class="num">${yen(c.l)}</span><br>終値 <span class="num">${yen(c.c)}</span>`;
    this.drawPrice();
  }
}
