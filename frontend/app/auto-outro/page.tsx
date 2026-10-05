"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import { PERAN_DEVELOPER, apiFetch, usePeran, useSudahHidrasi } from "../components/auth";

/**
 * Auto Outro — khusus akun developer.
 *
 * Satu klik: latar bergerak dirender sendiri (gratis, tanpa AI/API/antre),
 * lalu renderer aplikasi menempelkan nama channel dan enam handle PERSIS di
 * atasnya. Bisa banyak sekaligus: tiap baris nama channel = satu outro, dan
 * baris yang sama di kotak sosmed = akun untuk channel itu. Hasilnya muncul
 * di bawah, satu kartu per outro, masing-masing dengan tombol unduh.
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

// Satu kiriman dibatasi supaya antrean render (satu per satu) tidak tertahan
// berjam-jam oleh satu orang.
const MAKS_SEKALIGUS = 50;

type Mode = "biasa" | "dpp";

type Job = {
  job_id: string;
  channel?: string;
  mode?: Mode;
  status: "queued" | "running" | "done" | "error" | "dibatalkan";
  langkah: string;
  progress: number;
  punya_video: boolean;
  message: string;
  logs: string[];
  seed?: number;
  gaya?: string;
  created?: number;
  updated?: number;
};

type Rencana = { channel: string; akun: Record<string, string> };

const LANGKAH: Record<string, string> = {
  antre: "Menunggu giliran",
  render: "Merender",
  audio: "Menempel audio",
  simpan: "Menyimpan",
  selesai: "Selesai",
  gagal: "Gagal",
  berhenti: "Dihentikan",
};

function aktif(j: Job): boolean {
  return j.status === "queued" || j.status === "running";
}

/** Baris-baris isian, dirapikan; baris kosong tetap dihitung (penentu urutan). */
function barisDari(teks: string): string[] {
  const baris = teks.split("\n").map((b) => b.trim());
  while (baris.length > 0 && baris[baris.length - 1] === "") baris.pop();
  return baris;
}

/**
 * Pasangkan tiap nama channel dengan akunnya. Baris ke-n di kotak sosmed milik
 * channel baris ke-n. Kotak sosmed yang hanya berisi SATU baris dipakai untuk
 * semua channel - akun yang sama untuk semuanya tidak perlu ditulis berulang.
 */
function susunRencana(channelTeks: string, akunTeks: Record<string, string>): Rencana[] {
  const channel = barisDari(channelTeks);
  const perSosmed = Object.fromEntries(SOSMED.map((s) => [s.kunci, barisDari(akunTeks[s.kunci] || "")]));
  const hasil: Rencana[] = [];
  channel.forEach((nama, i) => {
    if (!nama) return;
    const akun: Record<string, string> = {};
    for (const s of SOSMED) {
      const baris = perSosmed[s.kunci];
      akun[s.kunci] = baris.length === 1 ? baris[0] : baris[i] || "";
    }
    hasil.push({ channel: nama.slice(0, 80), akun });
  });
  return hasil;
}

async function bacaGalat(r: Response, cadangan: string): Promise<string> {
  try {
    const j = await r.json();
    return (j && (j.detail || j.error)) || cadangan;
  } catch {
    return cadangan;
  }
}

function namaBerkas(channel: string): string {
  return `outro-${channel.trim().replace(/\s+/g, "-") || "video"}.mp4`;
}

export default function AutoOutroPage() {
  const peran = usePeran();
  const siap = useSudahHidrasi();
  const bolehLihat = peran === PERAN_DEVELOPER;

  // "biasa": gaya acak, tiap tekan beda. "dpp": meniru outro TV Rakyat apa
  // adanya; nama channel ditulis melengkung di logonya, sosmed dari isian.
  const [mode, setMode] = useState<Mode>("biasa");
  const [channel, setChannel] = useState("");
  const [akun, setAkun] = useState<Record<string, string>>(KOSONG);
  const [jobs, setJobs] = useState<Job[]>([]);
  const hilangRef = useRef(new Map<string, number>());
  const [galatJob, setGalatJob] = useState("");
  const [mengirim, setMengirim] = useState<{ ke: number; dari: number } | null>(null);
  const [videoUrl, setVideoUrl] = useState<Record<string, string>>({});
  const videoUrlRef = useRef<Record<string, string>>({});
  const diambilRef = useRef<Set<string>>(new Set());
  // Video yang gagal diambil dicoba lagi dengan jeda memanjang. Dulu yang
  // gagal tidak pernah dicoba lagi begitu semua outro selesai (pemantauan
  // berhenti), jadi kartunya tertahan di "Mengambil video..." selamanya.
  const gagalRef = useRef<Record<string, { coba: number; lagi: number }>>({});
  const [detak, setDetak] = useState(0);
  const [cobaVideo, setCobaVideo] = useState<Record<string, number>>({});

  const rencana = useMemo(() => susunRencana(channel, akun), [channel, akun]);
  const jumlahChannel = barisDari(channel).length;

  // Kotak sosmed yang jumlah barisnya tidak cocok dengan jumlah channel -
  // hampir pasti salah urut, jadi diberi tahu sebelum dikirim.
  const tidakCocok = useMemo(
    () =>
      SOSMED.filter((s) => {
        const n = barisDari(akun[s.kunci] || "").length;
        return n > 1 && n !== jumlahChannel;
      }).map((s) => s.nama),
    [akun, jumlahChannel],
  );

  const adaAktif = jobs.some(aktif);

  const perbarui = useCallback(async () => {
    try {
      const r = await apiFetch(`/api/outro/jobs?batas=200`);
      if (!r.ok) return;
      const data = (await r.json()) as { jobs: Job[] };
      const terbaru = new Map(data.jobs.map((j) => [j.job_id, j]));
      const hilang = hilangRef.current;
      setJobs((lama) =>
        lama.map((j) => {
          const baru = terbaru.get(j.job_id);
          if (baru) {
            hilang.delete(j.job_id);
            return baru;
          }
          if (!aktif(j)) return j;
          // Server sudah menjawab tapi job ini tidak ada: proses backend mati
          // dan job tidak bisa dilanjutkan. Tanpa ini kartunya "Menunggu giliran"
          // selamanya. Beberapa putaran dulu, karena backend yang baru menyala
          // butuh sebentar untuk memulihkan job yang tertunda.
          const kali = (hilang.get(j.job_id) || 0) + 1;
          hilang.set(j.job_id, kali);
          if (kali < 15) return j;
          return {
            ...j,
            status: "error",
            langkah: "gagal",
            message: "Terputus: server sempat mati dan outro ini tidak bisa dilanjutkan. Kirim ulang.",
          };
        }),
      );
    } catch {
      // dicoba lagi pada putaran berikutnya
    }
  }, []);

  // Pantau semua outro kiriman ini lewat SATU permintaan daftar.
  useEffect(() => {
    if (!adaAktif) return;
    const timer = setInterval(perbarui, 2000);
    return () => clearInterval(timer);
  }, [adaAktif, perbarui]);

  // Chrome memperlambat pewaktu tab yang tidak terlihat (pindah tab, jendela
  // tertutup jendela lain) sampai sekali per MENIT, dan menunda memuat video
  // di tab itu. Saat kembali, halaman tampak macet sampai giliran berikutnya.
  // Maka begitu tab terlihat lagi, status dan video langsung disusul.
  useEffect(() => {
    function saatTerlihat() {
      if (document.visibilityState !== "visible") return;
      for (const g of Object.values(gagalRef.current)) g.lagi = 0;
      void perbarui();
      setDetak((d) => d + 1);
    }
    document.addEventListener("visibilitychange", saatTerlihat);
    window.addEventListener("focus", saatTerlihat);
    return () => {
      document.removeEventListener("visibilitychange", saatTerlihat);
      window.removeEventListener("focus", saatTerlihat);
    };
  }, [perbarui]);

  // Outro yang SELESAI ditarik videonya sebagai blob, satu per satu, supaya
  // bisa diputar dan diunduh tanpa menempelkan token di alamat.
  useEffect(() => {
    const sekarang = Date.now();
    const perlu = jobs.filter(
      (j) =>
        j.status === "done" &&
        j.punya_video &&
        !diambilRef.current.has(j.job_id) &&
        (gagalRef.current[j.job_id]?.lagi ?? 0) <= sekarang,
    );
    if (perlu.length === 0) return;
    for (const j of perlu) diambilRef.current.add(j.job_id);
    void (async () => {
      for (const j of perlu) {
        let ok = false;
        try {
          const v = await apiFetch(`/api/outro/jobs/${j.job_id}/video`);
          if (v.ok) {
            const url = URL.createObjectURL(await v.blob());
            videoUrlRef.current[j.job_id] = url;
            setVideoUrl((lama) => ({ ...lama, [j.job_id]: url }));
            delete gagalRef.current[j.job_id];
            ok = true;
          }
        } catch {
          // ditangani di bawah
        }
        if (!ok) {
          const coba = (gagalRef.current[j.job_id]?.coba ?? 0) + 1;
          const jeda = Math.min(30000, 2000 * 2 ** (coba - 1));
          gagalRef.current[j.job_id] = { coba, lagi: Date.now() + jeda };
          setCobaVideo((lama) => ({ ...lama, [j.job_id]: coba }));
          diambilRef.current.delete(j.job_id);
          setTimeout(() => setDetak((d) => d + 1), jeda);
        }
      }
    })();
  }, [jobs, detak]);

  // Blob dilepas saat halaman ditinggalkan.
  useEffect(() => {
    const simpanan = videoUrlRef.current;
    return () => {
      for (const url of Object.values(simpanan)) URL.revokeObjectURL(url);
    };
  }, []);

  const jalankan = useCallback(async () => {
    if (rencana.length === 0 || mengirim) return;
    if (rencana.length > MAKS_SEKALIGUS) {
      setGalatJob(`Maksimal ${MAKS_SEKALIGUS} channel sekali kirim (sekarang ${rencana.length}).`);
      return;
    }
    setGalatJob("");
    let queueTicket = "";
    try {
      const antre = await apiFetch("/api/resource-queue/tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: `Auto Outro (${rencana.length})`, jumlah: rencana.length }),
      });
      if (!antre.ok) throw new Error(await bacaGalat(antre, "Tidak bisa masuk antrean server."));
      queueTicket = (await antre.json()).ticket_id;
    } catch (error) {
      setGalatJob(error instanceof Error ? error.message : "Tidak bisa masuk antrean server.");
      return;
    }
    const baru: Job[] = [];
    const galat: string[] = [];
    for (let i = 0; i < rencana.length; i++) {
      const r0 = rencana[i];
      setMengirim({ ke: i + 1, dari: rencana.length });
      try {
        const r = await apiFetch("/api/outro/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel: r0.channel, akun: r0.akun, mode, queue_ticket: queueTicket }),
        });
        if (!r.ok) {
          galat.push(`${r0.channel}: ${await bacaGalat(r, "gagal memulai")}`);
          void apiFetch(`/api/resource-queue/tickets/${queueTicket}/skip`, { method: "POST" });
          continue;
        }
        const j = await r.json();
        const job: Job = {
          job_id: j.job_id,
          channel: r0.channel,
          status: "queued",
          langkah: "antre",
          progress: 0,
          punya_video: false,
          message: "Menunggu giliran.",
          logs: [],
          seed: j.seed,
          gaya: j.gaya,
          mode: j.mode,
        };
        baru.push(job);
        // Kartunya langsung muncul satu per satu, tidak menunggu semua terkirim.
        setJobs((lama) => [...lama, job]);
      } catch {
        galat.push(`${r0.channel}: backend tidak merespons`);
        void apiFetch(`/api/resource-queue/tickets/${queueTicket}/skip`, { method: "POST" });
      }
    }
    setMengirim(null);
    if (galat.length > 0) setGalatJob(galat.join(" · "));
  }, [rencana, mode, mengirim]);

  async function hentikanSemua() {
    await Promise.all(
      jobs.filter(aktif).map((j) =>
        apiFetch(`/api/outro/jobs/${j.job_id}/stop`, { method: "POST" }).catch(() => undefined),
      ),
    );
  }

  function unduhSemua() {
    // Satu per satu dengan jeda: peramban menolak banyak unduhan serentak.
    const siapUnduh = jobs.filter((j) => videoUrl[j.job_id]);
    siapUnduh.forEach((j, i) => {
      setTimeout(() => {
        const a = document.createElement("a");
        a.href = videoUrl[j.job_id];
        a.download = namaBerkas(j.channel || "");
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, i * 400);
    });
  }

  function kosongkan() {
    setChannel("");
    setAkun(KOSONG);
    setJobs([]);
    setGalatJob("");
    for (const url of Object.values(videoUrlRef.current)) URL.revokeObjectURL(url);
    videoUrlRef.current = {};
    diambilRef.current = new Set();
    gagalRef.current = {};
    setCobaVideo({});
    setVideoUrl({});
  }

  // Server merender SATU per SATU; tanpa keterangan ini antrean panjang
  // (34 channel ~ 6 menit) terlihat seperti macet.
  const antre = jobs
    .filter((j) => j.status === "queued")
    .sort((a, b) => (a.created ?? 0) - (b.created ?? 0));
  const posisiAntre = new Map(antre.map((j, i) => [j.job_id, i + 1]));
  const sedangRender = jobs.find((j) => j.status === "running");
  const waktuSelesai = jobs
    .filter((j) => j.status === "done" && j.updated)
    .map((j) => j.updated as number)
    .sort((a, b) => a - b);
  const rataDetik =
    waktuSelesai.length >= 2
      ? Math.max(3, (waktuSelesai[waktuSelesai.length - 1] - waktuSelesai[0]) / (waktuSelesai.length - 1))
      : 12;
  const sisaDetik = Math.round((antre.length + (sedangRender ? 1 : 0)) * rataDetik);
  const sisaTeks = sisaDetik >= 60 ? `±${Math.ceil(sisaDetik / 60)} menit lagi` : `±${sisaDetik} detik lagi`;

  const bisaJalan = rencana.length > 0 && !mengirim;
  const selesai = jobs.filter((j) => j.status === "done").length;
  const gagal = jobs.filter((j) => j.status === "error").length;
  const siapUnduh = jobs.filter((j) => videoUrl[j.job_id]).length;

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
              <div className={styles.aksi}>
                <button
                  type="button"
                  className={mode === "biasa" ? styles.btnUtama : styles.btnSekunder}
                  onClick={() => setMode("biasa")}
                  disabled={!!mengirim}
                >
                  Biasa
                </button>
                <button
                  type="button"
                  className={mode === "dpp" ? styles.btnUtama : styles.btnSekunder}
                  onClick={() => setMode("dpp")}
                  disabled={!!mengirim}
                >
                  DPP
                </button>
              </div>
              {mode === "biasa" ? (
                <p className={styles.hint}>
                  Isi nama channel dan akun tiap sosmed, lalu tekan sekali. Tiap outro gayanya
                  berbeda — palet, latar, bentuk logo, tata letak, animasi — tapi logo, nama TV,
                  dan sosmednya selalu ditulis persis. Gratis, tanpa batas.
                </p>
              ) : (
                <p className={styles.hint}>
                  Persis outro TV Rakyat: latar, gerakan, tagline, dan pil emasnya sama. Yang
                  berbeda hanya logonya — logo TV Rakyat asli dengan nama channel ditulis
                  melengkung di bawah RAKYAT, mis. &quot;Aceh Barat&quot; — dan sosmed dari isian.
                </p>
              )}

              <div className={styles.form}>
                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama channel</h3>
                    <span className={styles.jumlahHuruf}>{rencana.length} outro</span>
                  </div>
                  <p className={styles.hint}>
                    Satu baris = satu channel = satu video outro. Bisa banyak sekaligus (maks{" "}
                    {MAKS_SEKALIGUS}).
                  </p>
                  <textarea
                    className={styles.naskah}
                    value={channel}
                    onChange={(e) => setChannel(e.target.value)}
                    placeholder={mode === "dpp" ? "Aceh Barat\nAceh Besar\nKab. Siak" : "TV Rakyat\nTV Nusantara"}
                    rows={Math.min(10, Math.max(3, jumlahChannel + 1))}
                    disabled={!!mengirim}
                  />
                </div>

                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama akun tiap sosmed</h3>
                  </div>
                  <p className={styles.hint}>
                    Baris ke-1 untuk channel baris ke-1, baris ke-2 untuk channel ke-2, dan
                    seterusnya. Isi <b>satu baris saja</b> kalau akunnya sama untuk semua channel.
                    Baris kosong = sosmed itu tidak ditampilkan di outro channel tersebut.
                  </p>
                  {SOSMED.map((s) => {
                    const n = barisDari(akun[s.kunci] || "").length;
                    return (
                      <div key={s.kunci} className={styles.row}>
                        <label>
                          <span>
                            {s.ikon} {s.nama}
                            {n > 0 && (
                              <>
                                {" "}
                                · {n === 1 && jumlahChannel > 1 ? "sama untuk semua" : `${n} baris`}
                              </>
                            )}
                          </span>
                          <textarea
                            className={styles.naskah}
                            value={akun[s.kunci] || ""}
                            onChange={(e) => setAkun((lama) => ({ ...lama, [s.kunci]: e.target.value }))}
                            placeholder={s.contoh}
                            rows={Math.min(8, Math.max(1, n))}
                            disabled={!!mengirim}
                          />
                        </label>
                      </div>
                    );
                  })}
                </div>

                {tidakCocok.length > 0 && (
                  <p className={styles.galat}>
                    Jumlah baris {tidakCocok.join(", ")} tidak sama dengan jumlah channel (
                    {jumlahChannel}). Cek urutannya di pratinjau di bawah.
                  </p>
                )}

                {rencana.length > 1 && (
                  <div className={styles.rincianKotak}>
                    {rencana.map((r, i) => {
                      const isi = SOSMED.filter((s) => r.akun[s.kunci]).map(
                        (s) => `${s.ikon} ${r.akun[s.kunci]}`,
                      );
                      return (
                        <div key={i} className={styles.rincianBaris}>
                          <span className={styles.rincianNilai}>
                            <b>
                              {i + 1}. {r.channel}
                            </b>{" "}
                            — {isi.length > 0 ? isi.join("  ") : "tanpa sosmed"}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}

                {galatJob && <p className={styles.galat}>{galatJob}</p>}
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnUtama}
                    onClick={jalankan}
                    disabled={!bisaJalan}
                    title={bisaJalan ? "Buat outro" : "Isi nama channel dulu"}
                  >
                    {mengirim
                      ? `Mengirim ${mengirim.ke}/${mengirim.dari}…`
                      : rencana.length > 1
                        ? `Buat ${rencana.length} Outro`
                        : "Buat Outro"}
                  </button>
                  {adaAktif ? (
                    <button type="button" className={styles.btnBahaya} onClick={hentikanSemua}>
                      Hentikan semua
                    </button>
                  ) : (
                    <button
                      type="button"
                      className={styles.btnSekunder}
                      onClick={kosongkan}
                      disabled={!!mengirim}
                    >
                      Kosongkan
                    </button>
                  )}
                </div>
              </div>
            </section>

            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Hasil</h2>
                {jobs.length > 0 && (
                  <span className={`${styles.badge} ${!adaAktif && gagal === 0 ? styles.sasaranOk : ""}`}>
                    {selesai}/{jobs.length} selesai{gagal > 0 ? ` · ${gagal} gagal` : ""}
                  </span>
                )}
              </div>
              {jobs.length === 0 ? (
                <p className={styles.hint}>Videonya akan muncul di sini setelah tombol ditekan.</p>
              ) : (
                <>
                  {adaAktif && (
                    <>
                      <div className={styles.progresBatang}>
                        <div
                          className={styles.progresIsi}
                          style={{ width: `${Math.round((selesai / Math.max(1, jobs.length)) * 100)}%` }}
                        />
                      </div>
                      <p className={styles.hint}>
                        {sedangRender ? (
                          <>
                            Sedang dirender: <b>{sedangRender.channel}</b> ({sedangRender.progress}%)
                          </>
                        ) : (
                          "Menunggu giliran di server"
                        )}
                        {" · "}
                        {antre.length} antre · {sisaTeks}. Dirender satu per satu; boleh
                        ditinggal, hasilnya tetap tersimpan.
                      </p>
                    </>
                  )}
                  {siapUnduh > 1 && (
                    <div className={styles.aksi}>
                      <button type="button" className={styles.btnUtama} onClick={unduhSemua}>
                        Unduh semua ({siapUnduh})
                      </button>
                    </div>
                  )}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
                      gap: 12,
                    }}
                  >
                    {jobs.map((j) => (
                      <div key={j.job_id} className={styles.rincianKotak}>
                        <div className={styles.blokHead}>
                          <h3 style={{ overflowWrap: "anywhere" }}>{j.channel}</h3>
                          <span
                            className={`${styles.badge} ${
                              j.status === "done"
                                ? styles.sasaranOk
                                : j.status === "error"
                                  ? styles.sasaranGagal
                                  : ""
                            }`}
                          >
                            {LANGKAH[j.langkah] || j.langkah}
                          </span>
                        </div>
                        {aktif(j) && (
                          <div className={styles.progresBatang}>
                            <div className={styles.progresIsi} style={{ width: `${j.progress}%` }} />
                          </div>
                        )}
                        {videoUrl[j.job_id] ? (
                          <>
                            <video
                              src={videoUrl[j.job_id]}
                              controls
                              loop
                              muted
                              playsInline
                              preload="metadata"
                              style={{ width: "100%", borderRadius: 10, background: "#000" }}
                            />
                            <a
                              className={styles.btnUtama}
                              href={videoUrl[j.job_id]}
                              download={namaBerkas(j.channel || "")}
                            >
                              Unduh
                            </a>
                          </>
                        ) : (
                          <p className={j.status === "error" ? styles.galat : styles.hint}>
                            {j.status === "done"
                              ? (cobaVideo[j.job_id] ?? 0) > 0
                                ? `Mengambil video… (coba lagi ke-${cobaVideo[j.job_id]})`
                                : "Mengambil video…"
                              : j.status === "queued" && posisiAntre.has(j.job_id)
                                ? `Antrean ke-${posisiAntre.get(j.job_id)}`
                                : j.message}
                          </p>
                        )}
                        {j.mode !== "dpp" && j.gaya && j.status === "done" && (
                          <p className={styles.hint}>
                            Gaya #{j.seed}: {j.gaya}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
