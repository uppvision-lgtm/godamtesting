"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../components/auth";

/**
 * Tab login penuh: menampilkan LAYAR PERAMBAN SERVER dan meneruskan klik &
 * ketikan pengguna ke sana. Halaman login platform (Instagram, dst) berjalan
 * di server; di sini hanya gambarnya. Pengguna login dan mengerjakan captcha
 * langsung di halaman itu — password tidak pernah lewat situs ini.
 *
 * Dibuka lewat window.open dari Kelola Akun dengan query:
 *   ?platform=instagram&sesi=akun-2&nama=Instagram
 */

type Status = "starting" | "running" | "done" | "error";

const NAMA_PLATFORM: Record<string, string> = {
  instagram: "Instagram",
  threads: "Threads",
  twitter: "X",
  facebook: "Facebook",
  tiktok: "TikTok",
};

export default function LoginSosmedPage() {
  const [pesan, setPesan] = useState("Menyiapkan halaman login…");
  const [status, setStatus] = useState<Status>("starting");
  const [ukuran, setUkuran] = useState({ lebar: 440, tinggi: 780 });
  const [frameUrl, setFrameUrl] = useState("");
  const tokenRef = useRef("");
  const antrean = useRef<Record<string, unknown>[]>([]);
  const areaRef = useRef<HTMLDivElement>(null);

  const q =
    typeof window !== "undefined" ? new URLSearchParams(window.location.search) : new URLSearchParams();
  const platform = (q.get("platform") || "instagram").toLowerCase();
  const sesi = q.get("sesi") || "ssid";
  const namaPlatform = NAMA_PLATFORM[platform] || platform;

  // Koordinat layar di dalam <img> -> koordinat piksel peramban server.
  const keServer = (e: { clientX: number; clientY: number }) => {
    const el = areaRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * ukuran.lebar;
    const y = ((e.clientY - r.top) / r.height) * ukuran.tinggi;
    if (x < 0 || y < 0 || x > ukuran.lebar || y > ukuran.tinggi) return null;
    return { x: Math.round(x), y: Math.round(y) };
  };

  const kirim = useCallback(async (events: Record<string, unknown>[]) => {
    const tk = tokenRef.current;
    if (!tk || !events.length) return;
    try {
      await apiFetch(`/api/login-layar/${tk}/input`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events }),
      });
    } catch {
      // satu paket gagal tidak fatal; peristiwa berikutnya menyusul
    }
  }, []);

  // Mulai sesi sekali.
  useEffect(() => {
    let batal = false;
    void (async () => {
      try {
        const res = await apiFetch("/api/login-layar/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform, lebar: 440, tinggi: 780 }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Gagal membuka halaman login.");
        if (batal) return;
        tokenRef.current = data.token;
        setUkuran({ lebar: data.lebar, tinggi: data.tinggi });
        setStatus("running");
        setPesan(`Login ${namaPlatform} di bawah ini. Kalau ada captcha, kerjakan langsung di sini.`);
      } catch (err) {
        setStatus("error");
        setPesan(err instanceof Error ? err.message : "Gagal membuka halaman login.");
      }
    })();
    return () => {
      batal = true;
      const tk = tokenRef.current;
      if (tk) void apiFetch(`/api/login-layar/${tk}/close`, { method: "POST" });
    };
  }, [platform, namaPlatform]);

  // Tarik frame ~5x/detik selama berjalan.
  useEffect(() => {
    if (status !== "running") return;
    let batal = false;
    let alamatLama = "";
    const timer = window.setInterval(async () => {
      const tk = tokenRef.current;
      if (!tk) return;
      try {
        const r = await apiFetch(`/api/login-layar/${tk}/frame`, { cache: "no-store" });
        if (!r.ok || r.status === 204 || batal) return;
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        setFrameUrl((lama) => {
          if (lama) URL.revokeObjectURL(lama);
          return url;
        });
        alamatLama = url;
      } catch {
        // dilewati; percobaan berikutnya menyusul
      }
    }, 200);
    return () => {
      batal = true;
      window.clearInterval(timer);
      if (alamatLama) URL.revokeObjectURL(alamatLama);
    };
  }, [status]);

  // Kirim peristiwa terkumpul tiap 60  md (gerak mouse bisa sangat sering).
  useEffect(() => {
    if (status !== "running") return;
    const timer = window.setInterval(() => {
      if (antrean.current.length) {
        const paket = antrean.current;
        antrean.current = [];
        void kirim(paket);
      }
    }, 60);
    return () => window.clearInterval(timer);
  }, [status, kirim]);

  // Pantau status; saat done, simpan sesinya ke akun pengguna lalu tutup tab.
  useEffect(() => {
    if (status !== "running") return;
    let batal = false;
    const timer = window.setInterval(async () => {
      const tk = tokenRef.current;
      if (!tk) return;
      try {
        const r = await apiFetch(`/api/login-layar/${tk}/status`);
        if (!r.ok || batal) return;
        const d = await r.json();
        if (d.status === "done" && d.session_id) {
          window.clearInterval(timer);
          setStatus("done");
          setPesan("Login berhasil. Menyimpan…");
          const simpan = await apiFetch("/api/sosmed/accounts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              platform,
              display_name: namaPlatform,
              session_name: sesi,
              session_id: d.session_id,
              extra: d.nama_akun ? { nama_akun: d.nama_akun } : {},
            }),
          });
          if (simpan.ok) {
            setPesan(`${namaPlatform}${d.nama_akun ? ` (${d.nama_akun})` : ""} tersimpan. Tab menutup…`);
            try {
              window.opener?.postMessage({ jenis: "login-sosmed-selesai", platform }, "*");
            } catch {
              /* opener beda origin: diabaikan */
            }
            setTimeout(() => window.close(), 1800);
          } else {
            const e = await simpan.json().catch(() => ({}));
            setStatus("error");
            setPesan(e.detail || "Login berhasil tapi gagal disimpan.");
          }
        } else if (d.status === "error") {
          window.clearInterval(timer);
          setStatus("error");
          setPesan(d.pesan || "Login gagal.");
        }
      } catch {
        // dicoba lagi
      }
    }, 1200);
    return () => {
      batal = true;
      window.clearInterval(timer);
    };
  }, [status, platform, sesi, namaPlatform]);

  const fokuskan = () => areaRef.current?.focus();

  return (
    <div
      style={{
        minHeight: "100dvh",
        background: "#0b0b0d",
        color: "#e8e8ea",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 460,
          padding: "10px 14px",
          fontSize: 13,
          lineHeight: 1.35,
          color: status === "error" ? "#ff8f8f" : "#c9c9cf",
          textAlign: "center",
        }}
      >
        {pesan}
        <div style={{ fontSize: 11, color: "#7d7d86", marginTop: 2 }}>
          Login {namaPlatform} lewat server godamtesting — password tidak disimpan.
        </div>
      </div>

      {status === "running" && (
        <div
          ref={areaRef}
          tabIndex={0}
          onMouseDown={fokuskan}
          onClick={(e) => {
            const p = keServer(e);
            if (p) void kirim([{ t: "click", x: p.x, y: p.y }]);
          }}
          onMouseMove={(e) => {
            const p = keServer(e);
            if (p) antrean.current.push({ t: "move", x: p.x, y: p.y });
          }}
          onWheel={(e) => antrean.current.push({ t: "wheel", dx: e.deltaX, dy: e.deltaY })}
          onKeyDown={(e) => {
            const cetak = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
            if (cetak) {
              antrean.current.push({ t: "text", v: e.key });
            } else if (["Enter", "Backspace", "Tab", "ArrowLeft", "ArrowRight", "Delete"].includes(e.key)) {
              e.preventDefault();
              antrean.current.push({ t: "key", k: e.key });
            }
          }}
          style={{
            width: "min(440px, 100vw)",
            aspectRatio: `${ukuran.lebar} / ${ukuran.tinggi}`,
            outline: "none",
            borderRadius: 10,
            overflow: "hidden",
            background: "#fff",
            boxShadow: "0 0 0 1px #2a2a2e, 0 10px 40px rgba(0,0,0,.5)",
            cursor: "pointer",
            touchAction: "none",
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          {frameUrl ? (
            <img
              src={frameUrl}
              alt="layar login"
              draggable={false}
              style={{ width: "100%", height: "100%", display: "block", userSelect: "none" }}
            />
          ) : (
            <div style={{ padding: 40, textAlign: "center", color: "#888" }}>Memuat…</div>
          )}
        </div>
      )}

      {status !== "running" && (
        <button
          type="button"
          onClick={() => window.close()}
          style={{
            marginTop: 20,
            padding: "10px 20px",
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
  );
}
