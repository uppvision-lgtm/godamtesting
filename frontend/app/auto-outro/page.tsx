"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import { PERAN_DEVELOPER, apiFetch, usePeran, useSudahHidrasi } from "../components/auth";

/**
 * Auto Outro — khusus akun developer, khusus backend lokal (mode via PC).
 *
 * Satu klik: AI menulis prompt Google Flow, prompt itu ditempel ke Flow,
 * videonya dibuat, lalu diunduh. Hanya Flow yang butuh akun masuk - jendela
 * peramban dibuka sekali, developer login sendiri, profilnya tersimpan untuk
 * seterusnya. Penulis promptnya lewat API, jadi tidak ada yang perlu
 * di-login untuk langkah pertama.
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

type Chrome = { tersambung: boolean; chrome_jalan: boolean; alamat: string };

type Job = {
  job_id: string;
  status: "queued" | "running" | "done" | "error" | "dibatalkan";
  langkah: string;
  progress: number;
  prompt: string;
  punya_video: boolean;
  message: string;
  logs: string[];
};

const LANGKAH: Record<string, string> = {
  antre: "Menunggu giliran",
  ai: "1/2 · AI menulis prompt",
  flow: "2/2 · Membuat video di Google Flow",
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

  const [lokal, setLokal] = useState<boolean | null>(null);
  const [chrome, setChrome] = useState<Chrome | null>(null);
  const [sibukSesi, setSibukSesi] = useState(false);
  const [galatSesi, setGalatSesi] = useState("");
  const [perluTutup, setPerluTutup] = useState(false);

  const [channel, setChannel] = useState("");
  const [akun, setAkun] = useState<Record<string, string>>(KOSONG);
  const [job, setJob] = useState<Job | null>(null);
  const [galatJob, setGalatJob] = useState("");
  const [mengirim, setMengirim] = useState(false);
  const [videoUrl, setVideoUrl] = useState("");
  const videoUrlRef = useRef("");

  const muatSesi = useCallback(async () => {
    try {
      const r = await apiFetch("/api/outro/sesi");
      if (!r.ok) {
        setGalatSesi(await bacaGalat(r, "Tidak bisa membaca status sesi."));
        return;
      }
      const j = await r.json();
      setLokal(Boolean(j.lokal));
      setChrome(j.chrome || null);
      setGalatSesi("");
    } catch {
      setGalatSesi("Backend tidak merespons.");
    }
  }, []);

  // Status dibaca setelah halaman hidup di browser. Pembacaannya ditaruh di
  // dalam fungsi async supaya keadaan tidak diubah serentak dengan render -
  // itu memicu rantai render yang tidak perlu.
  useEffect(() => {
    if (!siap || !bolehLihat) return;
    let batal = false;
    void (async () => {
      if (!batal) await muatSesi();
    })();
    return () => {
      batal = true;
    };
  }, [siap, bolehLihat, muatSesi]);

  /** Siapkan Chrome berport debug. `tutupDulu` menutup Chrome yang terbuka. */
  async function siapkanChrome(tutupDulu = false) {
    setSibukSesi(true);
    setGalatSesi("");
    try {
      const r = await apiFetch(`/api/outro/chrome?tutup_dulu=${tutupDulu ? "true" : "false"}`, {
        method: "POST",
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        setGalatSesi(j.detail || "Gagal menyiapkan Chrome.");
      } else {
        setPerluTutup(Boolean(j.perlu_tutup));
        if (!j.ok) setGalatSesi(j.pesan || "");
      }
    } catch {
      setGalatSesi("Backend tidak merespons.");
    } finally {
      setSibukSesi(false);
      void muatSesi();
    }
  }

  // Pantau pekerjaan sampai berhenti; begitu videonya ada, tarik sebagai blob
  // supaya bisa diputar dan diunduh tanpa menempelkan token di alamat.
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    let batal = false;
    const timer = setInterval(async () => {
      try {
        const r = await apiFetch(`/api/outro/jobs/${job.job_id}`);
        if (!r.ok || batal) return;
        const j = (await r.json()) as Job;
        setJob(j);
        if (j.status === "done" && j.punya_video) {
          const v = await apiFetch(`/api/outro/jobs/${j.job_id}/video`);
          if (v.ok && !batal) {
            const blob = await v.blob();
            if (videoUrlRef.current) URL.revokeObjectURL(videoUrlRef.current);
            const url = URL.createObjectURL(blob);
            videoUrlRef.current = url;
            setVideoUrl(url);
          }
        }
      } catch {
        // dicoba lagi pada detik berikutnya
      }
    }, 3000);
    return () => {
      batal = true;
      clearInterval(timer);
    };
  }, [job]);

  async function jalankan() {
    if (!channel.trim() || mengirim) return;
    setMengirim(true);
    setGalatJob("");
    setVideoUrl("");
    try {
      const r = await apiFetch("/api/outro/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: channel.trim(), akun }),
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
        prompt: "",
        punya_video: false,
        message: "Menunggu giliran.",
        logs: [],
      });
    } catch {
      setGalatJob("Backend tidak merespons.");
    } finally {
      setMengirim(false);
    }
  }

  async function hentikan() {
    if (!job) return;
    try {
      await apiFetch(`/api/outro/jobs/${job.job_id}/stop`, { method: "POST" });
    } catch {
      // status berikutnya akan menunjukkan hasilnya
    }
  }

  function kosongkan() {
    setChannel("");
    setAkun(KOSONG);
    setJob(null);
    setGalatJob("");
    if (videoUrlRef.current) URL.revokeObjectURL(videoUrlRef.current);
    videoUrlRef.current = "";
    setVideoUrl("");
  }

  const semuaMasuk = Boolean(chrome?.tersambung);
  const sedangJalan = !!job && ["queued", "running"].includes(job.status);
  const bisaJalan = channel.trim().length > 0 && semuaMasuk && lokal === true && !sedangJalan;

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
            {/* ---------- Chrome pengguna ---------- */}
            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Chrome kamu</h2>
                <span className={styles.badge}>Developer</span>
              </div>
              <p className={styles.hint}>
                Google menolak login dari peramban yang dikendalikan otomasi, jadi alat ini
                tidak login ke mana pun. Yang dipakai adalah <b>Chrome milikmu sendiri</b> yang
                sudah masuk akun Google — Flow dibuka di sana lewat port debug.
              </p>
              {lokal === false && (
                <p className={styles.peringatan}>
                  Auto Outro hanya berjalan di backend lokal (mode via PC). Nyalakan backend di
                  komputermu dan buka halaman ini dari sana.
                </p>
              )}
              {galatSesi && <p className={styles.galat}>{galatSesi}</p>}
              <div className={styles.rincianKotak}>
                <div className={styles.rincianBaris}>
                  <span className={styles.rincianNama}>
                    🌐 Chrome{" "}
                    <span className={chrome?.tersambung ? styles.sasaranOk : styles.sasaranGagal}>
                      {chrome?.tersambung ? "siap dipakai" : "belum siap"}
                    </span>
                  </span>
                  <span className={styles.rincianNilai}>
                    {chrome?.tersambung ? (
                      <button
                        type="button"
                        className={styles.btnSekunder}
                        disabled={sibukSesi}
                        onClick={() => siapkanChrome(false)}
                      >
                        Periksa lagi
                      </button>
                    ) : (
                      <button
                        type="button"
                        className={styles.btnUtama}
                        disabled={sibukSesi || lokal === false}
                        onClick={() => siapkanChrome(false)}
                      >
                        {sibukSesi ? "Menyiapkan…" : "Siapkan Chrome"}
                      </button>
                    )}
                  </span>
                </div>
              </div>
              {perluTutup && !chrome?.tersambung && (
                <>
                  <p className={styles.peringatan}>
                    Chrome sedang terbuka tanpa port debug, dan Chrome hanya membaca setelan itu
                    saat dijalankan. Chrome perlu ditutup lalu dibuka kembali —{" "}
                    <b>tab-tabmu akan dipulihkan</b>, tapi simpan dulu yang sedang kamu kerjakan.
                  </p>
                  <div className={styles.aksi}>
                    <button
                      type="button"
                      className={styles.btnBahaya}
                      disabled={sibukSesi}
                      onClick={() => siapkanChrome(true)}
                    >
                      Tutup Chrome &amp; buka ulang
                    </button>
                  </div>
                </>
              )}
            </section>

            {/* ---------- isian ---------- */}
            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Auto Outro</h2>
              </div>
              <p className={styles.hint}>
                Isi nama channel dan nama akun tiap sosmed, lalu tekan sekali. AI menulis
                promptnya, Google Flow membuat videonya, dan hasilnya muncul di bawah.
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
                </div>

                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama akun tiap sosmed</h3>
                  </div>
                  <p className={styles.hint}>Yang dikosongkan ditulis &quot;-&quot; di promptnya.</p>
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
                    title={
                      !channel.trim()
                        ? "Isi nama channel dulu"
                        : !semuaMasuk
                          ? "Login ChatGPT dan Google Flow dulu"
                          : "Buat outro"
                    }
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

            {/* ---------- hasil ---------- */}
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
                        playsInline
                        style={{ width: "100%", maxWidth: 360, borderRadius: 12, background: "#000" }}
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

                  {job.prompt && (
                    <div className={styles.blok}>
                      <div className={styles.blokHead}>
                        <h3>Prompt dari AI</h3>
                      </div>
                      <textarea className={styles.naskah} value={job.prompt} readOnly rows={8} />
                    </div>
                  )}

                  {job.logs.length > 0 && (
                    <div className={styles.rincianKotak}>
                      {job.logs.slice(-12).map((baris, i) => (
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
