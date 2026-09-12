"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";

type LiveStatus = {
  status: string;
  message: string;
  logs: string[];
  result?: { post_url?: string; status?: string } | null;
};

type Konfigurasi = { supabase: boolean; max_video_mb: number; jenis: string[] };

// Batas badan permintaan fungsi serverless Vercel.
const BATAS_PROXY_MB = 4;

function apiUrl(path: string): string {
  if (typeof window === "undefined") return path;
  const pc = new URLSearchParams(window.location.search).get("mode") === "pc";
  return pc ? `http://localhost:8000${path}` : path;
}

const selesai = (status: string) => status === "completed" || status === "error";

export default function AutoUploadPage() {
  const [konfig, setKonfig] = useState<Konfigurasi | null>(null);
  const [berkas, setBerkas] = useState<File | null>(null);
  const [username, setUsername] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [caption, setCaption] = useState("");

  const [token, setToken] = useState("");
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [frameUrl, setFrameUrl] = useState("");
  const [tahap, setTahap] = useState("");
  const [galat, setGalat] = useState("");
  const [sibuk, setSibuk] = useState(false);
  const tokenRef = useRef("");

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch(apiUrl("/api/upload/config"), { cache: "no-store" });
        if (res.ok) setKonfig(await res.json());
      } catch {
        setGalat("Backend tidak merespons. Pastikan servernya berjalan.");
      }
    })();
  }, []);

  // ===== PANTAU PROSES UNGGAH =====
  const tarikStatus = useCallback(async () => {
    if (!tokenRef.current) return;
    try {
      const res = await fetch(apiUrl(`/api/live/${tokenRef.current}/status`), { cache: "no-store" });
      if (!res.ok) return;
      setStatus(await res.json());
    } catch {
      // coba lagi pada putaran berikutnya
    }
  }, []);

  useEffect(() => {
    if (!token || (status && selesai(status.status))) return;
    tokenRef.current = token;
    const timer = window.setInterval(() => {
      void tarikStatus();
      if (!document.hidden) setFrameUrl(`${apiUrl(`/api/live/${token}/frame`)}?t=${Date.now()}`);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [token, status, tarikStatus]);

  // Tutup browser di server kalau halaman ditinggalkan saat masih berjalan.
  useEffect(() => {
    const tutup = () => {
      if (tokenRef.current) {
        try {
          navigator.sendBeacon(apiUrl(`/api/live/${tokenRef.current}/close`), "");
        } catch {
          // abaikan
        }
      }
    };
    window.addEventListener("pagehide", tutup);
    return () => window.removeEventListener("pagehide", tutup);
  }, []);

  /** Kirim berkas ke Supabase (langsung dari browser) atau ke backend. */
  async function kirimBerkas(file: File): Promise<{ file_id?: string; storage_path?: string }> {
    if (konfig?.supabase) {
      setTahap("Mengunggah video ke penyimpanan ...");
      const res = await fetch(apiUrl(`/api/upload/signed?nama=${encodeURIComponent(file.name)}`), {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal menyiapkan penyimpanan.");
      const unggah = await fetch(data.url, {
        method: "PUT",
        headers: { authorization: `Bearer ${data.token}`, "x-upsert": "true" },
        body: file,
      });
      if (!unggah.ok) throw new Error("Gagal mengunggah video ke penyimpanan.");
      return { storage_path: data.path };
    }

    setTahap("Mengirim video ke server ...");
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(apiUrl("/api/upload/file"), { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Gagal mengirim video.");
    return { file_id: data.file_id };
  }

  async function mulai() {
    setGalat("");
    if (!berkas) return setGalat("Pilih dulu video dari laptopmu.");
    if (!username.trim()) return setGalat("Isi nama akun Instagram-nya.");
    if (!sessionId.trim()) return setGalat("Session ID wajib diisi.");

    setSibuk(true);
    setStatus(null);
    setFrameUrl("");
    try {
      const sumber = await kirimBerkas(berkas);
      setTahap("Membuka Instagram ...");
      const res = await fetch(apiUrl("/api/upload/start"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: username.trim(),
          session_id: sessionId.trim(),
          caption,
          ...sumber,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal memulai unggahan.");
      setToken(data.token);
      setTahap("");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai unggahan.");
      setTahap("");
    } finally {
      setSibuk(false);
    }
  }

  async function hentikan() {
    if (!tokenRef.current) return;
    try {
      await fetch(apiUrl(`/api/live/${tokenRef.current}/close`), { method: "POST" });
    } catch {
      // abaikan
    }
    tokenRef.current = "";
    setToken("");
    setStatus(null);
    setFrameUrl("");
  }

  const berjalan = !!token && !(status && selesai(status.status));
  const tautan = status?.result?.post_url || "";
  const terlaluBesar =
    !!berkas && !konfig?.supabase && berkas.size > BATAS_PROXY_MB * 1_048_576;

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · AUTO UPLOAD</span>
        <span className={styles.headStatus}>{status?.status || (berjalan ? "memulai" : "idle")}</span>
      </header>

      <main className={styles.main}>
        <section className={styles.panel}>
          <h1>Unggah Video ke Instagram</h1>
          <p className={styles.sub}>
            Pilih video dari laptop, tempel Session ID, tulis caption, lalu biarkan
            botnya mengunggah. Prosesnya bisa kamu tonton langsung di bawah.
          </p>

          <div className={styles.form}>
            <label className={styles.filePicker}>
              <input
                type="file"
                accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm"
                onChange={(e) => setBerkas(e.target.files?.[0] || null)}
              />
              <span className={styles.fileBtn}>Pilih video</span>
              <span className={styles.fileName}>
                {berkas
                  ? `${berkas.name} — ${(berkas.size / 1_048_576).toFixed(1)} MB`
                  : "belum ada berkas dipilih"}
              </span>
            </label>

            {terlaluBesar && (
              <p className={styles.warn}>
                Video ini {(berkas.size / 1_048_576).toFixed(1)} MB, sedangkan lewat
                website batasnya {BATAS_PROXY_MB} MB. Nyalakan penyimpanan Supabase di
                server, atau pakai mode via PC.
              </p>
            )}

            <label>
              <span>Nama akun Instagram</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="mis. tokoku"
                autoComplete="off"
              />
            </label>

            <label>
              <span>Session ID</span>
              <input
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
                placeholder="tempel sessionid cookie"
                autoComplete="off"
              />
            </label>

            <label>
              <span>Caption</span>
              <textarea
                rows={4}
                value={caption}
                onChange={(e) => setCaption(e.target.value)}
                placeholder="Tulis caption di sini. Boleh pakai hashtag."
                maxLength={2200}
              />
              <span className={styles.hitung}>{caption.length}/2200</span>
            </label>

            {galat && <p className={styles.error} role="alert">{galat}</p>}
            {tahap && <p className={styles.tahap}>{tahap}</p>}

            <button className={styles.mulaiBtn} onClick={mulai} disabled={sibuk || berjalan}>
              {berjalan ? "Sedang mengunggah..." : sibuk ? "Menyiapkan..." : "Unggah ke Instagram"}
            </button>
          </div>
        </section>

        {token && (
          <section className={styles.panel}>
            <div className={styles.liveHead}>
              <h2>Chrome Live</h2>
              <span className={`${styles.badge} ${styles[status?.status || ""] || ""}`}>
                {status?.status || "memulai"}
              </span>
              <button className={styles.stopBtn} onClick={hentikan}>
                {berjalan ? "Stop" : "Tutup"}
              </button>
            </div>
            <p className={styles.pesan}>{status?.message}</p>

            {tautan && (
              <a className={styles.tautan} href={tautan} target="_blank" rel="noreferrer">
                Buka postingan yang baru diunggah →
              </a>
            )}

            <div className={styles.stage}>
              {frameUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={frameUrl} alt="Layar bot" className={styles.frame} draggable={false} />
              ) : (
                <p className={styles.hint}>Menunggu frame...</p>
              )}
            </div>

            <div className={styles.logBody}>
              {(status?.logs || []).map((baris, i) => (
                <p key={i}>{baris}</p>
              ))}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
