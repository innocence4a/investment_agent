import type { ServerMessage } from "./types";

/** 自動再接続付き WebSocket クライアント */
export function connect(
  onMessage: (msg: ServerMessage) => void,
  onStatus: (connected: boolean) => void,
): void {
  const token = new URLSearchParams(location.search).get("token") ?? "";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  let backoff = 1000;

  const open = (): void => {
    const ws = new WebSocket(url);
    ws.addEventListener("open", () => {
      backoff = 1000;
      onStatus(true);
    });
    ws.addEventListener("message", (ev: MessageEvent<string>) => {
      try {
        onMessage(JSON.parse(ev.data) as ServerMessage);
      } catch {
        // 不正メッセージは無視
      }
    });
    ws.addEventListener("close", () => {
      onStatus(false);
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 15_000);
    });
    ws.addEventListener("error", () => ws.close());
  };
  open();
}

/** 緊急停止 API(F-13 の入口。状態はコア側が保持する) */
export async function requestHalt(halted: boolean): Promise<void> {
  const token = new URLSearchParams(location.search).get("token") ?? "";
  await fetch("/api/halt", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ halted }),
  });
}
