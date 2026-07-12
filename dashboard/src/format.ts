// 表示フォーマット: 金額は等幅・桁揃え、時刻は JST 表示(内部は UTC)

export const yen = (v: number): string => "¥" + Math.round(v).toLocaleString("ja-JP");

export const signedYen = (v: number): string => (v >= 0 ? "+" : "−") + yen(Math.abs(v));

export const signedPct = (v: number): string =>
  (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%";

export const jstTime = (iso: string): string =>
  new Date(iso).toLocaleTimeString("ja-JP", { hour12: false, timeZone: "Asia/Tokyo" });

export const nowJst = (): string =>
  new Date().toLocaleTimeString("ja-JP", { hour12: false, timeZone: "Asia/Tokyo" });

/** タイムフレームに応じた X 軸ラベル(JST) */
export function tfTimeLabel(iso: string, tfMins: number): string {
  const d = new Date(iso);
  if (tfMins < 1440) {
    return d.toLocaleTimeString("ja-JP", {
      hour12: false, hour: "2-digit", minute: "2-digit", timeZone: "Asia/Tokyo",
    });
  }
  const parts = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", year: "2-digit", month: "numeric", day: "numeric",
  }).formatToParts(d);
  const get = (t: string): string => parts.find((p) => p.type === t)?.value ?? "";
  if (tfMins < 43200) return `${get("month")}/${get("day")}`;
  return `${get("year")}/${get("month")}`;
}

export function uptimeLabel(startedIso: string): string {
  const sec = Math.max(0, Math.floor((Date.now() - Date.parse(startedIso)) / 1000));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
