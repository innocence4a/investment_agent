import "./style.css";
import { Charts } from "./charts";
import { jstTime, nowJst, signedPct, signedYen, uptimeLabel, yen } from "./format";
import { AppState } from "./state";
import { TIMEFRAMES } from "./types";
import type { Fill, Symbol_, Thought, ThoughtKind } from "./types";
import { connect, requestHalt } from "./ws";

const $ = <T extends HTMLElement = HTMLElement>(id: string): T =>
  document.getElementById(id) as T;

const state = new AppState();
const charts = new Charts(state);

const SYM_LABEL: Record<Symbol_, string> = { BTC_JPY: "BTC / JPY", ETH_JPY: "ETH / JPY" };

// ── 思考ログ(F-11: 主役機能)──────────────────────
const KIND_META: Record<ThoughtKind, { label: string; chip: string }> = {
  buy: { label: "買い", chip: "buy" },
  sell: { label: "売り", chip: "sell" },
  close: { label: "売り", chip: "sell" },
  skip: { label: "見送り", chip: "skip" },
  risk: { label: "抑制", chip: "risk" },
  system: { label: "システム", chip: "skip" },
};

function esc(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function thoughtHtml(t: Thought): string {
  // 未知の kind はコアの更新が先行したケース。system 扱いにフォールバックし、
  // 1 件の不正データで描画ループ全体が止まらないようにする
  const meta = (KIND_META as Record<string, { label: string; chip: string }>)[t.kind]
    ?? KIND_META.system;
  const footer =
    t.kind === "system"
      ? ""
      : `<div class="conf">${
          t.confidence !== null ? `確信度 <b class="num">${Number(t.confidence)}%</b> · ` : ""
        }リスクゲート: ${esc(t.gate ?? "—")}</div>`;
  return `<div class="row1"><span class="chip ${meta.chip}">${meta.label}</span>
    <span class="agent">${esc(t.agent)}${
      t.symbol ? ` · ${esc(t.symbol.replace("_", "/"))}` : ""
    }</span>
    <span class="t num">${jstTime(t.ts)}</span></div>
    <p>${esc(t.text)}</p>${footer}`;
}

function renderThoughts(): void {
  const ul = $("thoughtLog");
  ul.innerHTML = "";
  for (const t of state.thoughts.slice(0, 30)) {
    const li = document.createElement("li");
    li.className = "thought";
    li.dataset["id"] = String(t.id);
    li.innerHTML = thoughtHtml(t);
    ul.appendChild(li);
  }
}

// ── 約定フィード(F-12)────────────────────────────
function fillHtml(f: Fill): string {
  const cls = f.side === "buy" ? "pos" : "neg";
  const mark = f.side === "buy" ? "▲ 買" : "▼ 売";
  const pnl =
    f.realized_pnl !== null
      ? ` <span class="${f.realized_pnl >= 0 ? "pos" : "neg"} num">${signedYen(f.realized_pnl)}</span>`
      : "";
  return `<span class="t num">${jstTime(f.ts)}</span>
    <span class="${cls}" style="font-weight:700">${mark}</span>
    <span>${esc(f.symbol.split("_")[0] ?? "")} <span class="num">${esc(f.qty)}</span></span>
    <span class="amt num">${yen(f.price)}${pnl}</span>`;
}

function renderFills(): void {
  const ul = $("feedList");
  ul.innerHTML = "";
  for (const f of state.fills.slice(0, 20)) {
    const li = document.createElement("li");
    li.className = "fill";
    li.innerHTML = fillHtml(f);
    ul.appendChild(li);
  }
}

// ── ポジション(F-12)──────────────────────────────
function renderPositions(): void {
  const tb = $("posBody");
  const rows = state.positions.filter((p) => Number(p.qty) > 0);
  if (rows.length === 0) {
    tb.innerHTML =
      '<tr><td colspan="6" style="text-align:center;color:var(--muted)">' +
      "ポジションなし — 次の判断サイクルを待機中</td></tr>";
    $("posMeta").textContent = "0 件";
    return;
  }
  tb.innerHTML = rows
    .map((p) => {
      const price = state.prices[p.symbol] ?? 0;
      const upnl = Math.round((price - p.avg_cost) * Number(p.qty));
      const cls = upnl >= 0 ? "pos" : "neg";
      return `<tr><td>${esc(p.symbol.replace("_", "/"))}</td><td class="dir pos">ロング</td>
        <td class="num">${esc(p.qty)}</td><td class="num">${yen(p.avg_cost)}</td>
        <td class="num">${yen(price)}</td><td class="num ${cls}">${signedYen(upnl)}</td></tr>`;
    })
    .join("");
  $("posMeta").textContent = `${rows.length} 件`;
}

// ── KPI ヘッダ(F-8)───────────────────────────────
function setSigned(id: string, v: number, text: string): void {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle("pos", v >= 0);
  el.classList.toggle("neg", v < 0);
}

function renderKpi(): void {
  const k = state.kpi;
  if (!k) return;
  $("kEquity").textContent = yen(k.equity);
  $("kEquitySub").textContent = `開始 ${yen(k.start_capital)}`;
  setSigned("kPnl", k.pnl_total, signedYen(k.pnl_total));
  setSigned("kPnlPct", k.pnl_total, signedPct((k.pnl_total / k.start_capital) * 100));
  setSigned("kToday", k.pnl_today, signedYen(k.pnl_today));
  $("kTodaySub").textContent = `${k.wins} 勝 ${k.losses} 敗`;
  const n = k.wins + k.losses;
  $("kWin").textContent = n ? ((k.wins / n) * 100).toFixed(1) + "%" : "—";
  $("kTrades").textContent = `取引 ${k.trades} 回`;
}

// ── トップバー・接続状態 ───────────────────────────
function renderTopbar(): void {
  const cfg = state.config;
  document.body.classList.toggle("halted", state.halted);
  document.body.classList.toggle("disconnected", !state.connected);
  $("killBtn").textContent = state.halted ? "再開する" : "緊急停止";
  if (cfg) {
    $("modeBadge").textContent = cfg.mode; // PAPER / LIVE 常時表示(F-13)
    $("modeBadge").className = "badge " + (cfg.mode === "PAPER" ? "paper" : "live-mode");
    $("feedBadge").hidden = cfg.feed !== "sim";
    $("llmBadge").hidden = cfg.llm !== "mock";
    if (cfg.feed === "sim") {
      $("footNote").textContent =
        "検証モード — 表示中の価格はシミュレーションです。実際の市場データ・売買は含まれません。";
    }
  }
  const label = !state.connected
    ? "コアと切断 — 再接続中…"
    : state.halted
      ? "停止中(損切り・利確監視は継続)"
      : `稼働中 · 判断サイクル ${cfg ? Math.round(cfg.decision_interval_sec / 60 * 10) / 10 : "-"}分`;
  $("liveLabel").textContent = label;
}

const price = (): number | undefined => state.prices[state.activeSym];
function renderLastPrice(): void {
  const p = price();
  $("lastPrice").textContent = p !== undefined ? yen(p) : "";
}

// ── タブ(銘柄 / タイムフレーム)────────────────────
function buildTabs(): void {
  const symTabs = $("symTabs");
  (["BTC_JPY", "ETH_JPY"] as Symbol_[]).forEach((sym) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tf" + (sym === state.activeSym ? " on" : "");
    b.textContent = sym.split("_")[0] ?? sym;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => {
      state.activeSym = sym;
      $("chartTitle").textContent = SYM_LABEL[sym];
      [...symTabs.children].forEach((x) => x.classList.toggle("on", x === b));
      charts.drawAll();
      renderLastPrice();
    });
    symTabs.appendChild(b);
  });
  const tfTabs = $("tfTabs");
  TIMEFRAMES.forEach((tf) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tf" + (tf.key === state.activeTf ? " on" : "");
    b.textContent = tf.label;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => {
      state.activeTf = tf.key;
      [...tfTabs.children].forEach((x) => x.classList.toggle("on", x === b));
      charts.drawAll();
    });
    tfTabs.appendChild(b);
  });
}

// ── 緊急停止(F-13)────────────────────────────────
let opErrorTimer: number | undefined;
function showOpError(text: string): void {
  const el = $("opError");
  el.textContent = text;
  el.hidden = false;
  window.clearTimeout(opErrorTimer);
  opErrorTimer = window.setTimeout(() => {
    el.hidden = true;
  }, 10_000);
}

$("killBtn").addEventListener("click", () => {
  const next = !state.halted;
  const msg = next
    ? "緊急停止しますか?\n新規エントリーを停止します(損切り・利確監視は継続)。"
    : "取引を再開しますか?";
  if (!window.confirm(msg)) return;
  // 状態はコア側が保持し、halt メッセージで画面に反映される。
  // 失敗(コア停止・認証エラー等)は安全機能のためサイレントにせず必ず表示する
  void requestHalt(next).then((ok) => {
    if (!ok) {
      showOpError(
        (next ? "⚠ 緊急停止リクエストが失敗しました" : "⚠ 再開リクエストが失敗しました") +
          " — コアに接続できないか、認証に失敗しています。もう一度お試しください。",
      );
    }
  });
});

// ── 描画ループ(rAF で間引き)───────────────────────
let dirtyCharts = false;
let dirtyDom = false;
function scheduleRender(chartsChanged: boolean, domChanged: boolean): void {
  dirtyCharts ||= chartsChanged;
  dirtyDom ||= domChanged;
  requestAnimationFrame(() => {
    if (dirtyCharts) {
      charts.drawAll();
      renderLastPrice();
      dirtyCharts = false;
    }
    if (dirtyDom) {
      renderKpi();
      renderThoughts();
      renderFills();
      renderPositions();
      renderTopbar();
      dirtyDom = false;
    }
  });
}

connect(
  (msg) => {
    state.apply(msg);
    switch (msg.type) {
      case "snapshot":
        scheduleRender(true, true);
        break;
      case "tick":
        renderPositions(); // 評価損益は現在値で更新
        scheduleRender(true, false);
        break;
      case "equity":
        scheduleRender(true, false);
        break;
      default:
        scheduleRender(false, true);
    }
  },
  (connected) => {
    state.connected = connected;
    renderTopbar();
  },
);

buildTabs();
renderTopbar();
charts.drawAll();
window.addEventListener("resize", () => charts.drawAll());

// 時計・稼働時間(表示は JST)
setInterval(() => {
  $("clock").textContent = nowJst() + " JST";
  if (state.kpi) $("kUptime").textContent = uptimeLabel(state.kpi.started_at);
}, 1000);
