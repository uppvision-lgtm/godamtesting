"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import { PERAN_DEVELOPER, apiFetch, usePeran, useSudahHidrasi } from "../components/auth";

/**
 * Auto Outro — khusus akun developer.
 *
 * Satu klik: latar bergerak dirender sendiri (gratis, tanpa AI/API/antre),
 * lalu renderer aplikasi menempelkan nama channel dan enam handle PERSIS di
 * atasnya. Hasilnya muncul di bawah dengan tombol unduh.
 */

type Sosmed = { kunci: string; nama: string; ikon: string; contoh: string };

// Urutan isian sesuai permintaan: X, TikTok, Facebook, YouTube, Instagram, Threads.
const SOSMED: Sosmed[] = [
  { kunci: "x", nama: "X", ikon: "𝕏", contoh: "@namaakun" },
  { kunci: "tiktok", nama: "TikTok", ikon: "🎵", contoh: "@namaakun" },
  { kunci: "facebook", nama: "Facebook", ikon: "📘", contoh: "Nama Halaman" },
  { kunci: "youtube", nama: "YouTube", ikon: "▶️", contoh: "@namachannel" },
  { kunci: "instagram", nama: "Instagram", ikon: "📸", contoh: "@namaakun" },
  { kunci: "threads", nama: "Threads", ikon: "🧵", contoh: "@namaakun" },
];
const KOSONG: Record<string, string> = Object.fromEntries(SOSMED.map((s) => [s.kunci, ""]));

type Job = {
  job_id: string;
  status: "queued" | "running" | "done" | "error" | "dibatalkan";
  langkah: string;
  progress: number;
  punya_video: boolean;
  message: string;
  logs: string[];
};

const LANGKAH: Record<string, string> = {
  antre: "Menunggu giliran",
  render: "1/2 · Merender outro",
  audio: "2/2 · Menempel audio",
  selesai: "Selesai",
  gagal: "Gagal",
  berhenti: "Dihentikan",
};

async function bacaGalat(r: Response, cadangan: string): Promise<string> {
  try {
    const j = await r.json();
    return (j && (j.detail || j.error)) || cadangan;
  } catch {
    return cadangan;
  }
}

export default function AutoOutroPage() {
  const peran = usePeran();
  const siap = useSudahHidrasi();
  const bolehLihat = peran === PERAN_DEVELOPER;

  const [channel, setChannel] = useState("");
  const [tagline, setTagline] = useState("");
  const [ajakan, setAjakan] = useState("");
  const [akun, setAkun] = useState<Record<string, string>>(KOSONG);
  const [job, setJob] = useState<Job | null>(null);
  const [galatJob, setGalatJob] = useState("");
  const [mengirim, setMengirim] = useState(false);
  const [videoUrl, setVideoUrl] = useState("");
  const videoUrlRef = useRef("");

  // Pantau pekerjaan selama masih berjalan.
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    let batal = false;
    const timer = setInterval(async () => {
      try {
        const r = await apiFetch(`/api/outro/jobs/${job.job_id}`);
        if (!r.ok || batal) return;
        const j = (await r.json()) as Job;
        if (!batal) setJob(j);
      } catch {
        // dicoba lagi pada detik berikutnya
      }
    }, 2000);
    return () => {
      batal = true;
      clearInterval(timer);
    };
  }, [job]);

  // Begitu pekerjaan SELESAI, tarik videonya sebagai blob supaya bisa diputar
  // dan diunduh tanpa menempelkan token di alamat. Sengaja efek terpisah dari
  // pemantauan: dulu pengambilannya di dalam putaran pantau, dan saat status
  // berubah jadi "done" efek itu langsung dibersihkan - videonya keburu batal
  // sebelum sempat terpasang. Di sini ia berjalan setelah status jadi "done".
  useEffect(() => {
    if (!job || job.status !== "done" || !job.punya_video) return;
    let batal = false;
    void (async () => {
      try {
        const v = await apiFetch(`/api/outro/jobs/${job.job_id}/video`);
        if (!v.ok || batal) return;
        const blob = await v.blob();
        if (batal) return;
        if (videoUrlRef.current) URL.revokeObjectURL(videoUrlRef.current);
        const url = URL.createObjectURL(blob);
        videoUrlRef.current = url;
        setVideoUrl(url);
      } catch {
        // tombol unduh langsung tetap disediakan sebagai cadangan
      }
    })();
    return () => {
      batal = true;
    };
  }, [job]);

  const jalankan = useCallback(async () => {
    if (!channel.trim() || mengirim) return;
    setMengirim(true);
    setGalatJob("");
    setVideoUrl("");
    try {
      const r = await apiFetch("/api/outro/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: channel.trim(), tagline: tagline.trim(), ajakan: ajakan.trim(), akun }),
      });
      if (!r.ok) {
        setGalatJob(await bacaGalat(r, "Gagal memulai."));
        return;
      }
      const j = await r.json();
      setJob({
        job_id: j.job_id,
        status: "queued",
        langkah: "antre",
        progress: 0,
        punya_video: false,
        message: "Menunggu giliran.",
        logs: [],
      });
    } catch {
      setGalatJob("Backend tidak merespons.");
    } finally {
      setMengirim(false);
    }
  }, [channel, tagline, ajakan, akun, mengirim]);

  async function hentikan() {
    if (!job) return;
    try {
      await apiFetch(`/api/outro/jobs/${job.job_id}/stop`, { method: "POST" });
    } catch {
      // status berikutnya menunjukkan hasilnya
    }
  }

  function kosongkan() {
    setChannel("");
    setTagline("");
    setAjakan("");
    setAkun(KOSONG);
    setJob(null);
    setGalatJob("");
    if (videoUrlRef.current) URL.revokeObjectURL(videoUrlRef.current);
    videoUrlRef.current = "";
    setVideoUrl("");
  }

  const sedangJalan = !!job && ["queued", "running"].includes(job.status);
  const bisaJalan = channel.trim().length > 0 && !sedangJalan;

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">
          ←
        </Link>
        <span className={styles.brand}>AUTOMASI · AUTO OUTRO</span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        {!siap ? (
          <section className={`${styles.panel} ${styles.panelLebar}`}>
            <p className={styles.hint}>Memuat…</p>
          </section>
        ) : !bolehLihat ? (
          <section className={`${styles.panel} ${styles.panelLebar}`}>
            <div className={styles.panelHead}>
              <h2>Khusus developer</h2>
            </div>
            <p className={styles.hint}>
              Halaman ini hanya untuk akun developer. Masuk dengan akun developer untuk
              membukanya.
            </p>
            <Link href="/" className={styles.tautan}>
              ← Kembali ke beranda
            </Link>
          </section>
        ) : (
          <>
            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Auto Outro</h2>
                <span className={styles.badge}>Developer</span>
              </div>
              <p className={styles.hint}>
                Isi nama channel, tagline, dan akun tiap sosmed, lalu tekan sekali. Outronya
                dirender sendiri meniru gaya TV Rakyat (gratis, tanpa batas), dan nama serta
                username ditulis persis.
              </p>

              <div className={styles.form}>
                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama channel</h3>
                  </div>
                  <div className={styles.row}>
                    <label>
                      <span>Nama channel</span>
                      <input
                        type="text"
                        value={channel}
                        onChange={(e) => setChannel(e.target.value)}
                        placeholder="TV Rakyat"
                        maxLength={80}
                        disabled={sedangJalan}
                      />
                    </label>
                  </div>
                  <div className={styles.row}>
                    <label>
                      <span>Tagline (opsional)</span>
                      <input
                        type="text"
                        value={tagline}
                        onChange={(e) => setTagline(e.target.value)}
                        placeholder="TV Rakyat, Medianya Rakyat"
                        maxLength={80}
                        disabled={sedangJalan}
                      />
                    </label>
                    <label>
                      <span>Teks ajakan</span>
                      <input
                        type="text"
                        value={ajakan}
                        onChange={(e) => setAjakan(e.target.value)}
                        placeholder="Follow untuk Update Terkini"
                        maxLength={60}
                        disabled={sedangJalan}
                      />
                    </label>
                  </div>
                </div>

                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama akun tiap sosmed</h3>
                  </div>
                  <p className={styles.hint}>Yang dikosongkan tidak ditampilkan di outronya.</p>
                  {SOSMED.map((s) => (
                    <div key={s.kunci} className={styles.row}>
                      <label>
                        <span>
                          {s.ikon} {s.nama}
                        </span>
                        <input
                          type="text"
                          value={akun[s.kunci] || ""}
                          onChange={(e) => setAkun((lama) => ({ ...lama, [s.kunci]: e.target.value }))}
                          placeholder={s.contoh}
                          maxLength={80}
                          disabled={sedangJalan}
                        />
                      </label>
                    </div>
                  ))}
                </div>

                {galatJob && <p className={styles.galat}>{galatJob}</p>}
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnUtama}
                    onClick={jalankan}
                    disabled={!bisaJalan || mengirim}
                    title={bisaJalan ? "Buat outro" : "Isi nama channel dulu"}
                  >
                    {mengirim ? "Memulai…" : "Buat Outro"}
                  </button>
                  {sedangJalan ? (
                    <button type="button" className={styles.btnBahaya} onClick={hentikan}>
                      Hentikan
                    </button>
                  ) : (
                    <button type="button" className={styles.btnSekunder} onClick={kosongkan}>
                      Kosongkan
                    </button>
                  )}
                </div>
              </div>
            </section>

            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Hasil</h2>
                {job && (
                  <span
                    className={`${styles.badge} ${
                      job.status === "done"
                        ? styles.sasaranOk
                        : job.status === "error"
                          ? styles.sasaranGagal
                          : ""
                    }`}
                  >
                    {LANGKAH[job.langkah] || job.langkah}
                  </span>
                )}
              </div>
              {!job ? (
                <p className={styles.hint}>Videonya akan muncul di sini setelah tombol ditekan.</p>
              ) : (
                <>
                  {sedangJalan && (
                    <div className={styles.progresBatang}>
                      <div className={styles.progresIsi} style={{ width: `${job.progress}%` }} />
                    </div>
                  )}
                  <p className={job.status === "error" ? styles.galat : styles.hint}>{job.message}</p>

                  {videoUrl && (
                    <div className={styles.rincianKotak}>
                      <video
                        src={videoUrl}
                        controls
                        autoPlay
                        loop
                        muted
                        playsInline
                        style={{ width: "100%", maxWidth: 300, borderRadius: 12, background: "#000" }}
                      />
                      <div className={styles.aksi}>
                        <a
                          className={styles.btnUtama}
                          href={videoUrl}
                          download={`outro-${channel.trim().replace(/\s+/g, "-") || "video"}.mp4`}
                        >
                          Unduh video
                        </a>
                      </div>
                    </div>
                  )}

                  {job.logs.length > 0 && (
                    <div className={styles.rincianKotak}>
                      {job.logs.slice(-10).map((baris, i) => (
                        <div key={i} className={styles.rincianBaris}>
                          <span className={styles.rincianNilai}>{baris}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
