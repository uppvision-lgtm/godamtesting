"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import { PERAN_DEVELOPER, apiFetch, usePeran, useSudahHidrasi } from "../components/auth";

/**
 * Auto Outro — khusus akun developer, khusus backend lokal (mode via PC).
 *
 * Satu klik menjalankan alur yang biasanya dikerjakan tangan: ChatGPT diminta
 * menulis prompt, prompt itu ditempel ke Google Flow, videonya dibuat, lalu
 * diunduh. Kedua situs butuh akun yang sudah masuk, jadi di atas isian ada
 * dua baris sesi: jendela peramban dibuka, developer login sendiri sekali,
 * dan profilnya tersimpan untuk seterusnya.
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

type Sesi = { running: boolean; message: string; has_session: boolean };
type PetaSesi = Record<string, Sesi>;
const PLATFORM: { kunci: string; nama: string; ikon: string }[] = [
  { kunci: "chatgpt", nama: "ChatGPT", ikon: "💬" },
  { kunci: "flow", nama: "Google Flow", ikon: "🎞️" },
];

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
  chatgpt: "1/2 · Meminta prompt ke ChatGPT",
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
  const [sesi, setSesi] = useState<PetaSesi>({});
  const [sibukSesi, setSibukSesi] = useState<string>("");
  const [galatSesi, setGalatSesi] = useState("");

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
      setSesi(j.sesi || {});
      setGalatSesi("");
    } catch {
      setGalatSesi("Backend tidak merespons.");
    }
  }, []);

  // Status sesi dimuat saat halaman dibuka, lalu dipantau selama ada jendela
  // login yang terbuka supaya pesannya ("silakan login...") ikut berubah.
  useEffect(() => {
    if (!siap || !bolehLihat) return;
    let batal = false;
    const ambil = async () => {
      if (!batal) await muatSesi();
    };
    void ambil();
    const timer = setInterval(() => {
      if (Object.values(sesi).some((s) => s.running)) void ambil();
    }, 3000);
    return () => {
      batal = true;
      clearInterval(timer);
    };
  }, [siap, bolehLihat, muatSesi, sesi]);

  async function aksiSesi(platform: string, aksi: "mulai" | "selesai" | "batal") {
    setSibukSesi(`${platform}:${aksi}`);
    setGalatSesi("");
    try {
      const r = await apiFetch(`/api/outro/sesi/${platform}/${aksi}`, { method: "POST" });
      if (!r.ok) setGalatSesi(await bacaGalat(r, "Gagal."));
    } catch {
      setGalatSesi("Backend tidak merespons.");
    } finally {
      setSibukSesi("");
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

  const semuaMasuk = PLATFORM.every((p) => sesi[p.kunci]?.has_session);
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
            {/* ---------- sesi login ---------- */}
            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Sesi peramban</h2>
                <span className={styles.badge}>Developer</span>
              </div>
              <p className={styles.hint}>
                ChatGPT dan Google Flow harus sudah masuk. Tekan <b>Buka jendela login</b>,
                login di jendela Chrome yang muncul, lalu tekan <b>Selesai</b>. Cukup sekali —
                sesinya tersimpan.
              </p>
              {lokal === false && (
                <p className={styles.peringatan}>
                  Auto Outro hanya berjalan di backend lokal (mode via PC). Nyalakan backend di
                  komputermu dan buka halaman ini dari sana.
                </p>
              )}
              {galatSesi && <p className={styles.galat}>{galatSesi}</p>}
              <div className={styles.rincianKotak}>
                {PLATFORM.map((p) => {
                  const s = sesi[p.kunci];
                  const sibuk = sibukSesi.startsWith(p.kunci + ":");
                  return (
                    <div key={p.kunci} className={styles.rincianBaris}>
                      <span className={styles.rincianNama}>
                        {p.ikon} {p.nama}{" "}
                        <span className={s?.has_session ? styles.sasaranOk : styles.sasaranGagal}>
                          {s?.has_session ? "masuk" : "belum masuk"}
                        </span>
                      </span>
                      <span className={styles.rincianNilai}>
                        {s?.running ? (
                          <>
                            <span className={styles.hint}>{s.message}</span>{" "}
                            <button
                              type="button"
                              className={styles.btnUtama}
                              disabled={sibuk}
                              onClick={() => aksiSesi(p.kunci, "selesai")}
                            >
                              Selesai
                            </button>{" "}
                            <button
                              type="button"
                              className={styles.btnSekunder}
                              disabled={sibuk}
                              onClick={() => aksiSesi(p.kunci, "batal")}
                            >
                              Batal
                            </button>
                          </>
                        ) : (
                          <>
                            {s?.message && !s.has_session && (
                              <span className={styles.hint}>{s.message} </span>
                            )}
                            <button
                              type="button"
                              className={s?.has_session ? styles.btnSekunder : styles.btnUtama}
                              disabled={sibuk || lokal === false}
                              onClick={() => aksiSesi(p.kunci, "mulai")}
                            >
                              {s?.has_session ? "Login ulang" : "Buka jendela login"}
                            </button>
                          </>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            </section>

            {/* ---------- isian ---------- */}
            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Auto Outro</h2>
              </div>
              <p className={styles.hint}>
                Isi nama channel dan nama akun tiap sosmed, lalu tekan sekali. ChatGPT
                menulis promptnya, Google Flow membuat videonya, dan hasilnya muncul di bawah.
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
                        <h3>Prompt dari ChatGPT</h3>
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
