"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { urlApi } from "./auth";

export type PresenceStats = {
  /** Jumlah tab yang sedang membuka website ini saat ini juga. */
  online: number;
  /** Jumlah script bot (Chromium) yang sedang benar-benar berjalan. */
  runningBots: number;
  /** false selama backend belum sempat menjawab (angka ditampilkan "—"). */
  ready: boolean;
};

const DEFAULT_STATS: PresenceStats = { online: 0, runningBots: 0, ready: false };
const PresenceContext = createContext<PresenceStats>(DEFAULT_STATS);

const VISITOR_KEY = "ig-tools-visitor-id";
// Jalur cadangan (dipakai hanya kalau SSE gagal berkali-kali, mis. diblokir proxy).
const FALLBACK_AFTER_ERRORS = 3;
const FALLBACK_POLL_MS = 5000;

export function usePresence(): PresenceStats {
  return useContext(PresenceContext);
}

/** ID unik per tab (sessionStorage), supaya 1 tab dihitung 1 pengguna. */
function getVisitorId(): string {
  const baru = () =>
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  try {
    const tersimpan = window.sessionStorage.getItem(VISITOR_KEY);
    if (tersimpan) return tersimpan;
    const id = baru();
    window.sessionStorage.setItem(VISITOR_KEY, id);
    return id;
  } catch {
    // Mode privat / storage diblokir: cukup pakai ID sekali pakai.
    return baru();
  }
}

export function PresenceProvider({ children }: { children: React.ReactNode }) {
  const [stats, setStats] = useState<PresenceStats>(DEFAULT_STATS);

  useEffect(() => {
    const visitorId = getVisitorId();
    let berhenti = false;
    let sumber: EventSource | null = null;
    let pollTimer = 0;
    let gagalBeruntun = 0;

    const terapkan = (data: unknown) => {
      if (berhenti || typeof data !== "object" || data === null) return;
      const isi = data as { online?: unknown; running_bots?: unknown };
      setStats({
        online: Number(isi.online) || 0,
        runningBots: Number(isi.running_bots) || 0,
        ready: true,
      });
    };

    // ===== JALUR CADANGAN: polling biasa =====
    const ping = async () => {
      try {
        const res = await fetch(urlApi("/api/presence/ping"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ visitor_id: visitorId }),
          cache: "no-store",
        });
        if (res.ok) terapkan(await res.json());
      } catch {
        // Coba lagi pada putaran berikutnya.
      }
    };

    const mulaiPolling = () => {
      if (pollTimer || berhenti) return;
      void ping();
      pollTimer = window.setInterval(() => void ping(), FALLBACK_POLL_MS);
    };

    // ===== JALUR UTAMA: server mendorong angka baru begitu ada perubahan =====
    const bukaStream = () => {
      if (berhenti || typeof EventSource === "undefined") {
        mulaiPolling();
        return;
      }
      sumber = new EventSource(urlApi(`/api/presence/stream?v=${encodeURIComponent(visitorId)}`));
      sumber.onmessage = (event) => {
        gagalBeruntun = 0;
        try {
          terapkan(JSON.parse(event.data));
        } catch {
          // Baris rusak diabaikan; baris berikutnya menyusul 1 detik lagi.
        }
      };
      sumber.onerror = () => {
        // Stream memang ditutup berkala lalu disambung ulang sendiri oleh
        // browser. Baru dianggap benar-benar gagal kalau tidak ada satu pun
        // data yang masuk setelah beberapa kali percobaan.
        gagalBeruntun += 1;
        if (gagalBeruntun >= FALLBACK_AFTER_ERRORS) {
          sumber?.close();
          sumber = null;
          mulaiPolling();
        }
      };
    };

    const keluar = () => {
      sumber?.close();
      sumber = null;
      try {
        // Blob TANPA tipe: sendBeacon lintas-asal dengan tipe application/json
        // ditolak peramban (butuh preflight). Tanpa Content-Type, backend
        // tetap membacanya sebagai JSON.
        navigator.sendBeacon(
          urlApi("/api/presence/leave"),
          new Blob([JSON.stringify({ visitor_id: visitorId })])
        );
      } catch {
        // abaikan
      }
    };

    // Saat kembali ke tab ini, pastikan angkanya segar lagi.
    const onVisibility = () => {
      if (document.hidden || berhenti) return;
      if (sumber === null && pollTimer === 0) bukaStream();
      else if (pollTimer) void ping();
    };

    bukaStream();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", keluar);

    return () => {
      berhenti = true;
      if (pollTimer) window.clearInterval(pollTimer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", keluar);
      keluar();
    };
  }, []);

  return <PresenceContext.Provider value={stats}>{children}</PresenceContext.Provider>;
}
