/* Original formatClock, DeepStudent 635216503c0b34bdf812e17942e0f09f51872e6f.
 * ir.ts: MIT License, Copyright (c) 2026 BA7MLV. Full original notice is in the accompanying upstream source. */
export function formatClock(totalSec: number): string {
  const sec = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
