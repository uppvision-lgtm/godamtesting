"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { BASIS_BACKEND, ambilToken, apiFetch } from "../components/auth";

/**
 * Tab login penuh: menampilkan LAYAR PERAMBAN SERVER dan meneruskan
 * klik/ketik/gulir pengguna ke sana lewat WebSocket. Halaman login platform
 * berjalan di server; password diketik langsung di sana, situs ini tidak
 * pernah menyentuhnya. Ukuran peramban server mengikuti ukuran tab (dan
 * kepadatan layar, retina 2x), jadi tajam dan responsif — termasuk setelah
 * login saat platform beralih ke tampilan desktop.
 */

type Status = "starting" | "running" | "done" | "error";

const NAMA_PLATFORM: Record<string, string> = {
  instagram: "Instagram",
  threads: "Threads",
  twitter: "X",
  facebook: "Facebook",
  tiktok: "TikTok",
};

const TOMBOL_KHUSUS = new Set([
  "Enter", "Backspace", "Tab", "Delete", "Escape",
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
]);

export default function LoginSosmedPage() {
  const [status, setStatus] = useState<Status>("starting");
  const [pesan, setPesan] = useState("Menyiapkan halaman login…");
  const kanvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const ukuranServer = useRef({ w: 1024, h: 760 }); // piksel CSS peramban server
  const seret = useRef(false);

  const q =
    typeof window !== "undefined" ? new URLSearchParams(window.location.search) : new URLSearchParams();
  const platform = (q.get("platform") || "instagram").toLowerCase();
  const sesi = q.get("sesi") || "ssid";
  const namaPlatform = NAMA_PLATFORM[platform] || platform;

  const kirim = useCallback((obj: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  // Koordinat pointer di dalam kanvas -> piksel CSS peramban server.
  const keServer = useCallback((e: { clientX: number; clientY: number }) => {
    const el = kanvasRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * ukuranServer.current.w;
    const y = ((e.clientY - r.top) / r.height) * ukuranServer.current.h;
    if (x < 0 || y < 0 || x > ukuranServer.current.w || y > ukuranServer.current.h) return null;
    return { x: Math.round(x), y: Math.round(y) };
  }, []);

  // Mulai sesi + buka WebSocket. Ukuran awal = ukuran wrapper x kepadatan layar.
  useEffect(() => {
    let batal = false;
    let ws: WebSocket | null = null;

    void (async () => {
      const wrap = wrapRef.current;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const lebar = Math.round(wrap ? wrap.clientWidth : Math.min(window.innerWidth, 1024));
      const tinggi = Math.round(wrap ? wrap.clientHeight : window.innerHeight - 60);
      const seluler = window.innerWidth < 720;
      try {
        const res = await apiFetch("/api/login-layar/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform, lebar, tinggi, dpr, seluler }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Gagal membuka halaman login.");
        if (batal) return;
        ukuranServer.current = { w: data.lebar, h: data.tinggi };
        // WebSocket harus LANGSUNG ke backend: proxy Next/Vercel tidak
        // meneruskan WebSocket (404). NEXT_PUBLIC_API_URL berisi alamat
        // backend publik di situs (Railway) dan localhost:8000 di laptop -
        // sama dengan yang dipakai unggahan langsung. BASIS_BACKEND dicoba
        // dulu kalau kebetulan diisi.
        const asal = (
          BASIS_BACKEND ||
          process.env.NEXT_PUBLIC_API_URL ||
          window.location.origin
        ).replace(/\/$/, "");
        const basis = asal.replace(/^http/, "ws");
        ws = new WebSocket(`${basis}/api/login-layar/${data.token}/ws?akses=${encodeURIComponent(ambilToken())}`);
        wsRef.current = ws;
        ws.onopen = () => {
          if (batal) return;
          setStatus("running");
          setPesan(`Login ${namaPlatform} di bawah. Kalau ada captcha, kerjakan langsung di sini.`);
        };
        ws.onmessage = (ev) => {
          const m = JSON.parse(ev.data);
          if (m.t === "frame") {
            ukuranServer.current = { w: m.w, h: m.h };
            gambar(m.b64, m.w, m.h);
          } else if (m.t === "selesai") {
            void selesaikan(m);
          }
        };
        ws.onclose = () => {
          if (!batal) setStatus((s) => (s === "done" ? s : "error"));
        };
      } catch (err) {
        if (!batal) {
          setStatus("error");
          setPesan(err instanceof Error ? err.message : "Gagal membuka halaman login.");
        }
      }
    })();

    return () => {
      batal = true;
      try {
        ws?.close();
      } catch {
        /* abaikan */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform, namaPlatform]);

  function gambar(b64: string, w: number, h: number) {
    const c = kanvasRef.current;
    if (!c) return;
    const img = new Image();
    img.onload = () => {
      if (c.width !== img.width || c.height !== img.height) {
        c.width = img.width;
        c.height = img.height;
      }
      // rasio kanvas mengikuti ukuran server supaya tidak gepeng
      c.style.aspectRatio = `${w} / ${h}`;
      const ctx = c.getContext("2d");
      ctx?.drawImage(img, 0, 0);
    };
    img.src = "data:image/jpeg;base64," + b64;
  }

  const selesaikan = useCallback(
    async (m: { status: string; pesan: string; session_id: string; nama_akun: string }) => {
      if (m.status !== "done" || !m.session_id) {
        setStatus("error");
        setPesan(m.pesan || "Login gagal.");
        return;
      }
      setStatus("done");
      setPesan("Login berhasil. Menyimpan…");
      const simpan = await apiFetch("/api/sosmed/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform,
          display_name: namaPlatform,
          session_name: sesi,
          session_id: m.session_id,
          extra: m.nama_akun ? { nama_akun: m.nama_akun } : {},
        }),
      });
      if (simpan.ok) {
        setPesan(`${namaPlatform}${m.nama_akun ? ` (${m.nama_akun})` : ""} tersimpan. Tab menutup…`);
        try {
          window.opener?.postMessage({ jenis: "login-sosmed-selesai", platform }, "*");
        } catch {
          /* opener beda origin */
        }
        setTimeout(() => window.close(), 1600);
      } else {
        const e = await simpan.json().catch(() => ({}));
        setStatus("error");
        setPesan(e.detail || "Login berhasil tapi gagal disimpan.");
      }
    },
    [platform, sesi, namaPlatform]
  );

  // Beri tahu server saat ukuran tab berubah, supaya layarnya ikut menyesuaikan.
  useEffect(() => {
    if (status !== "running") return;
    let jam = 0;
    const ro = new ResizeObserver(() => {
      window.clearTimeout(jam);
      jam = window.setTimeout(() => {
        const wrap = wrapRef.current;
        if (wrap) kirim({ t: "resize", lebar: Math.round(wrap.clientWidth), tinggi: Math.round(wrap.clientHeight) });
      }, 250);
    });
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => {
      window.clearTimeout(jam);
      ro.disconnect();
    };
  }, [status, kirim]);

  const masukanRef = useRef<HTMLInputElement>(null);
  // Di HP, kanvas tidak memunculkan keyboard; kolom tersembunyi ini yang
  // difokuskan, dan ketikannya diteruskan ke server.
  const fokus = () => {
    if (window.matchMedia("(pointer: coarse)").matches) masukanRef.current?.focus();
    else kanvasRef.current?.focus();
  };

  // Tempel (Cmd/Ctrl+V): isi clipboard di komputer pengguna dikirim sebagai
  // teks. Menekan Ctrl+V di server akan menempel clipboard SERVER yang kosong.
  useEffect(() => {
    if (status !== "running") return;
    const tempel = (e: ClipboardEvent) => {
      const teks = e.clipboardData?.getData("text") || "";
      if (teks) {
        e.preventDefault();
        kirim({ t: "text", v: teks });
      }
    };
    window.addEventListener("paste", tempel);
    return () => window.removeEventListener("paste", tempel);
  }, [status, kirim]);

  return (
    <div
      style={{
        height: "100dvh",
        background: "#0b0b0d",
        color: "#e8e8ea",
        display: "flex",
        flexDirection: "column",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <div style={{ padding: "8px 14px", textAlign: "center", flexShrink: 0 }}>
        <div style={{ fontSize: 13, color: status === "error" ? "#ff8f8f" : "#d7d7dc" }}>{pesan}</div>
        <div style={{ fontSize: 11, color: "#7d7d86", marginTop: 1 }}>
          Login {namaPlatform} lewat server godamtesting — password tidak disimpan.
        </div>
      </div>

      <div
        ref={wrapRef}
        style={{
          flex: 1,
          minHeight: 0,
          margin: "0 auto",
          width: "100%",
          maxWidth: 1100,
          display: "flex",
          alignItems: "stretch",
          justifyContent: "center",
          padding: "0 8px 8px",
        }}
      >
        {status === "running" && (
          <input
            ref={masukanRef}
            aria-hidden="true"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            style={{ position: "fixed", opacity: 0, width: 1, height: 1, left: 0, top: 0 }}
            onInput={(e) => {
              const el = e.currentTarget;
              if (el.value) kirim({ t: "text", v: el.value });
              el.value = "";
            }}
            onKeyDown={(e) => {
              if (TOMBOL_KHUSUS.has(e.key)) {
                e.preventDefault();
                kirim({ t: "key", k: e.key });
              }
            }}
          />
        )}
        {status === "running" ? (
          <canvas
            ref={kanvasRef}
            tabIndex={0}
            onMouseDown={(e) => {
              fokus();
              const p = keServer(e);
              if (p) {
                seret.current = true;
                kirim({ t: "down", x: p.x, y: p.y });
              }
            }}
            onMouseUp={(e) => {
              const p = keServer(e);
              if (p && seret.current) kirim({ t: "up", x: p.x, y: p.y });
              seret.current = false;
            }}
            onMouseMove={(e) => {
              if (!seret.current) return;
              const p = keServer(e);
              if (p) kirim({ t: "move", x: p.x, y: p.y });
            }}
            onWheel={(e) => kirim({ t: "wheel", dx: e.deltaX, dy: e.deltaY })}
            onKeyDown={(e) => {
              const cetak = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
              if (cetak) {
                kirim({ t: "text", v: e.key });
              } else if (TOMBOL_KHUSUS.has(e.key)) {
                e.preventDefault();
                kirim({ t: "key", k: e.key });
              }
            }}
            style={{
              maxWidth: "100%",
              maxHeight: "100%",
              height: "auto",
              borderRadius: 10,
              background: "#fff",
              boxShadow: "0 0 0 1px #2a2a2e, 0 10px 40px rgba(0,0,0,.5)",
              outline: "none",
              cursor: "default",
              touchAction: "none",
            }}
          />
        ) : (
          <div style={{ margin: "auto", textAlign: "center" }}>
            {status !== "starting" && (
              <button
                type="button"
                onClick={() => window.close()}
                style={{
                  padding: "10px 22px",
                  borderRadius: 8,
                  border: "none",
                  background: "#2f6fed",
                  color: "#fff",
                  fontSize: 14,
                  cursor: "pointer",
                }}
              >
                Tutup
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
