"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";
import { bacaForm, simpanForm } from "../components/formStore";

/**
 * Auto Edit Video.
 *
 * Alurnya sengaja dibuat satu arah supaya tidak ada yang perlu diketik:
 *
 *   tempel link -> keterangan videonya muncul -> hook 3 baris dibuat otomatis
 *   dari caption -> tekan Buat video -> hasilnya bisa langsung diputar.
 *
 * Layer tidak lagi berupa "template" yang harus dikelola. Susunannya tetap:
 * 4 layer + 1 outro, sesuai skrip main.py. Pengguna hanya mengganti berkasnya.
 */

// Satu susunan layer saja untuk seluruh aplikasi, jadi tidak ada daftar
// template yang harus dipilih. Id-nya tetap supaya berkas yang sudah diunggah
// tidak hilang saat halaman dibuka lagi.
const ID_LAYER = "utama";

const BATAS_UNGGAH_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB || 200);

type Overlay = {
  file: string;
  x: number | null;
  y: number | null;
  w?: number | null;
  h?: number | null;
  crop?: string | null;
  start?: number | null;
  end?: number | null;
  loop?: boolean;
  label?: string;
};

type TextLayer = {
  name: string;
  size?: number;
  color?: string;
  x?: number | null;
  y?: number | null;
  align?: string;
  stroke?: number;
  stroke_color?: string;
  line_spacing?: number;
  start?: number | null;
  end?: number | null;
  // Khusus style "berita": paragraf rata kiri-kanan, kata pertama berwarna.
  style?: string;
  width?: number;
  min_size?: number;
  max_lines?: number;
  line_height?: number;
  kicker_color?: string;
};

type Susunan = {
  id: string;
  name: string;
  width: number;
  height: number;
  fps: number;
  intro: string | null;
  outro: string | null;
  max_duration: number | null;
  overlays: Overlay[];
  texts: TextLayer[];
  assets?: string[];
};

type Pratinjau = {
  title: string;
  description: string;
  uploader: string;
  duration: number | null;
  thumbnail: string;
  extractor: string;
  width: number;
  height: number;
  view_count: number;
  like_count: number;
  upload_date: string;
  too_long: boolean;
  max_seconds: number;
};

type JobStatus = {
  job_id: string;
  status: string;
  progress: number;
  message?: string;
  logs?: string[];
  output?: string | null;
  url?: string | null;
  error?: string | null;
};

/** Nama slot yang tampil di layar, berurutan dari lapisan bawah ke atas.
 *  Nama diambil dari sini, bukan dari data tersimpan, supaya penggantian nama
 *  langsung terlihat tanpa perlu membuat ulang susunan layernya. */
const NAMA_SLOT = [
  "kotak monas",
  "kotak putih 3 detik awal",
  "boom like share",
  "bingkai teratas",
];

/**
 * Susunan 4 layer + outro persis seperti skrip main.py (kanvas 720x1280).
 * Angkanya diambil dari perintah ffmpeg di skrip itu supaya hasilnya sama.
 * Layer 2, layer 3, dan ketiga baris teks memakai jendela waktu yang sama
 * (detik 0-3): kalau teks dibiarkan lebih lama, ia melayang tanpa latar.
 */
/**
 * Paragraf hook di dalam kotak putih: "VIRAL! KONTROVERSI KARNAVAL DI ...".
 * Kata pertama merah, sisanya hitam mengalir di baris yang sama, rata
 * kiri-kanan, maksimal 4 baris. Kalau tidak muat, renderer menyusutkan font
 * sedikit demi sedikit sampai min_size.
 *
 * Angkanya diukur dari contoh acuan: pada kotak 700x283 teks mulai 28 px dari
 * kiri dan 83 px dari atas kotak (x = 10+28, y = 710+73 karena PIL menghitung
 * dari puncak baris, bukan puncak huruf). Lebar 660 menyisakan 12 px di
 * kanan — serapat contoh. Ukuran 38 dengan lebar itu memenggal kalimat contoh
 * persis seperti acuannya.
 */
const TEKS_HOOK: TextLayer = {
  name: "hook",
  style: "berita",
  size: 38,
  min_size: 22,
  max_lines: 4,
  x: 38,
  y: 783,
  width: 660,
  line_height: 1.15,
  color: "black",
  kicker_color: "#d32d27",
  start: 0,
  end: 3,
};

/** Layer teks kredit pemilik video. */
const TEKS_SUMBER: TextLayer = {
  name: "sumber",
  size: 10,
  x: null,
  // Duduk di dalam bar merah bawah milik layer "bingkai teratas", sebaris
  // dengan ikon media sosial. Bar itu ada di y 1239..1279 pada kanvas
  // 720x1280. Nilai y adalah puncak baris teks, bukan puncak hurufnya:
  // pada ukuran ini tinta jatuh sekitar 1..12 px di bawahnya tergantung
  // karakternya (garis miring menjulur paling tinggi), sehingga 1253
  // menempatkan semua kemungkinan nama tetap terpusat di dalam bar.
  y: 1253,
  align: "right",
  color: "white",
  // Garis tepi ikut dikecilkan: 3 px pada huruf setinggi 7 px akan menutupi
  // hurufnya sendiri.
  stroke: 1,
  stroke_color: "black",
};

function susunanBawaan(): Susunan {
  return {
    id: ID_LAYER,
    name: "Auto Edit Video",
    width: 720,
    height: 1280,
    fps: 30,
    intro: null,
    outro: null,
    max_duration: null,
    overlays: [
      // Panel bawah. Berkasnya 3888x2529 dengan pinggiran transparan di kanan;
      // isinya sendiri 3473x2529 alias berasio 1,373 — crop di sini hanya
      // membuang pinggiran itu, bukan memotong gambar, supaya monasnya tidak
      // gepeng. Ukuran 936x681 adalah 1,3 kali lebar layar supaya monasnya
      // tampil besar; sisi kiri-kanan yang lewat batas layar terpotong sendiri.
      //
      // Panel diturunkan 170 px (seperempat tingginya) supaya monasnya duduk
      // lebih rendah dan tidak tertutup kotak putih. Bagian bawahnya jadi
      // lewat batas layar dan terpotong — itu disengaja. Bar media sosial
      // bawaan panel ikut keluar layar, jadi yang terlihat hanya bar milik
      // layer "bingkai teratas" dan tidak ada bar dobel.
      //
      // w dan h tetap 1,3 kali ukuran asli (936x681 dari rasio 1,373), jadi
      // menggeser ini tidak meregangkan gambarnya.
      { label: NAMA_SLOT[0], file: "", x: -108, y: 782, w: 936, h: 681,
        crop: "3473:2529:0:0", start: 0, end: 3 },
      { label: NAMA_SLOT[1], file: "", x: 10, y: 710, w: 700, h: 283, start: 0, end: 3 },
      // loop: berkasnya berupa video pendek. Tanpa diulang, animasinya
      // membeku di frame terakhir begitu videonya habis. Pengulangan hanya
      // berlaku di badan video — outro disusun terpisah, jadi layer ini
      // tidak pernah muncul di sana.
      { label: NAMA_SLOT[2], file: "", x: 45, y: 55, w: 280, h: 158, loop: true },
      { label: NAMA_SLOT[3], file: "", x: 0, y: 63, w: 720, h: 1217 },
    ],
    texts: [TEKS_HOOK, TEKS_SUMBER],
    assets: [],
  };
}

/** Rangkai kata pembuka + isi jadi "VIRAL! ISI ..." seperti yang dirender. */
function susunHook(kicker: string, isi: string): string {
  const k = kicker.trim().replace(/!+$/, "").toUpperCase();
  const i = isi.trim().toUpperCase();
  if (!i) return "";
  return k ? `${k}! ${i}` : i;
}

function formatDurasi(detik: number): string {
  const total = Math.round(detik);
  const m = Math.floor(total / 60);
  const d = total % 60;
  return `${m}:${String(d).padStart(2, "0")}`;
}

function formatAngka(n: number): string {
  if (!n) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} jt`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} rb`;
  return String(n);
}

function formatTanggal(yyyymmdd: string): string {
  if (!/^\d{8}$/.test(yyyymmdd)) return "";
  return `${yyyymmdd.slice(6, 8)}/${yyyymmdd.slice(4, 6)}/${yyyymmdd.slice(0, 4)}`;
}

function selesai(status: string): boolean {
  return status === "done" || status === "error";
}

export default function EditVideoPage() {
  const awal = bacaForm("edit-video", { link: "" });

  const [link, setLink] = useState(awal.link);
  const [susunan, setSusunan] = useState<Susunan | null>(null);
  const [pesanLayer, setPesanLayer] = useState("");

  const [pratinjau, setPratinjau] = useState<Pratinjau | null>(null);
  const [memuatPratinjau, setMemuatPratinjau] = useState(false);
  const [galatPratinjau, setGalatPratinjau] = useState("");

  // "ai"     = disusun dari caption video sumber
  // "manual" = diketik sendiri
  const [modeTeks, setModeTeks] = useState<"ai" | "manual">("ai");
  // Kata pembuka (merah, diberi tanda seru) dan isi paragrafnya.
  const [kicker, setKicker] = useState("");
  const [isi, setIsi] = useState("");
  const [kickerManual, setKickerManual] = useState("");
  const [isiManual, setIsiManual] = useState("");
  const [sumberHook, setSumberHook] = useState("");
  const [membuatHook, setMembuatHook] = useState(false);

  const [job, setJob] = useState<JobStatus | null>(null);
  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");
  const jobRef = useRef<string>("");

  useEffect(() => {
    simpanForm("edit-video", { link });
  }, [link]);

  // ===== SUSUNAN LAYER =====
  const muatSusunan = useCallback(async () => {
    try {
      const res = await fetch(`/api/video/templates/${ID_LAYER}`, { cache: "no-store" });
      if (res.ok) {
        const ada: Susunan = await res.json();
        // Semua layer teks didefinisikan di kode (hook + kredit sumber), jadi
        // susunan tersimpan selalu disamakan dengannya. Dengan begini susunan
        // lama (mis. yang masih memakai 3 baris headline/detail/message)
        // otomatis ikut ke bentuk baru tanpa perlu dibuat ulang dan tanpa
        // mengunggah lagi berkas layernya.
        const tetap = [TEKS_HOOK, TEKS_SUMBER];
        const perluDisimpan = JSON.stringify(ada.texts || []) !== JSON.stringify(tetap);
        ada.texts = tetap;
        if (perluDisimpan) {
          await simpanSusunan(ada);
        } else {
          setSusunan(ada);
        }
        return;
      }
    } catch {
      // lanjut ke pembuatan di bawah
    }
    // Belum ada: buat sekali, lalu dipakai seterusnya.
    try {
      const bawaan = susunanBawaan();
      const res = await fetch(`/api/video/templates?template_id=${ID_LAYER}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bawaan),
      });
      if (res.ok) setSusunan(await res.json());
    } catch {
      setPesanLayer("Tidak bisa menyiapkan susunan layer.");
    }
  }, []);

  useEffect(() => {
    // Perubahan state terjadi setelah fetch selesai, bukan saat effect jalan.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void muatSusunan();
  }, [muatSusunan]);

  /** Simpan perubahan susunan (dipanggil setelah berkas layer diganti). */
  async function simpanSusunan(baru: Susunan) {
    setSusunan(baru);
    await fetch(`/api/video/templates?template_id=${ID_LAYER}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: baru.name,
        width: baru.width,
        height: baru.height,
        fps: baru.fps,
        outro: baru.outro,
        max_duration: baru.max_duration,
        overlays: baru.overlays,
        texts: baru.texts,
      }),
    });
  }

  /** Unggah satu berkas, kembalikan namanya ("assets/xxx.png"). */
  async function unggah(file: File): Promise<string | null> {
    if (file.size > BATAS_UNGGAH_MB * 1_048_576) {
      setPesanLayer(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar (batas ${BATAS_UNGGAH_MB} MB).`
      );
      return null;
    }
    setPesanLayer(`Mengunggah ${file.name} ...`);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`/api/video/templates/${ID_LAYER}/assets`, {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal mengunggah.");
      setPesanLayer(`${file.name} tersimpan.`);
      return data.file as string;
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Gagal mengunggah.");
      return null;
    }
  }

  async function gantiLayer(index: number, file: File) {
    if (!susunan) return;
    const nama = await unggah(file);
    if (!nama) return;
    const overlays = [...susunan.overlays];
    overlays[index] = { ...overlays[index], file: nama };
    await simpanSusunan({ ...susunan, overlays });
  }

  async function gantiOutro(file: File) {
    if (!susunan) return;
    const nama = await unggah(file);
    if (!nama) return;
    await simpanSusunan({ ...susunan, outro: nama });
  }

  // ===== BACA LINK & BUAT HOOK =====

  /** Ubah caption jadi 3 baris lower-third lewat backend. */
  const buatHook = useCallback(async (info: Pratinjau) => {
    const naskah = [info.title, info.description].filter(Boolean).join(". ");
    if (naskah.trim().length < 3) {
      setKicker("");
      setIsi("");
      setSumberHook("Caption di link ini kosong, jadi hook tidak bisa dibuat.");
      return;
    }
    setMembuatHook(true);
    try {
      const res = await fetch("/api/video/hook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ naskah }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal membuat hook.");
      setKicker(data.kicker || "");
      setIsi(data.body || "");
      setSumberHook(
        data.source === "deepseek"
          ? "Dibuat AI dari caption video."
          : "AI tidak aktif — diringkas langsung dari caption."
      );
    } catch (err) {
      setKicker("");
      setIsi("");
      setSumberHook(err instanceof Error ? err.message : "Gagal membuat hook.");
    } finally {
      setMembuatHook(false);
    }
  }, []);

  /** Baca isi link tanpa mengunduh videonya, lalu langsung buatkan hook. */
  const bacaLink = useCallback(
    async (tautan: string) => {
      if (!tautan.toLowerCase().startsWith("http")) {
        setPratinjau(null);
        setGalatPratinjau("");
        setKicker("");
        setIsi("");
        setSumberHook("");
        return;
      }
      setMemuatPratinjau(true);
      setGalatPratinjau("");
      try {
        const res = await fetch("/api/video/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: tautan }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Tidak bisa membaca link.");
        setPratinjau(data as Pratinjau);
        await buatHook(data as Pratinjau);
      } catch (err) {
        setPratinjau(null);
        setKicker("");
        setIsi("");
        setSumberHook("");
        setGalatPratinjau(err instanceof Error ? err.message : "Tidak bisa membaca link.");
      } finally {
        setMemuatPratinjau(false);
      }
    },
    [buatHook]
  );

  // Ditunda sebentar supaya server tidak dipanggil tiap satu huruf diketik.
  useEffect(() => {
    const timer = setTimeout(() => void bacaLink(link.trim()), 700);
    return () => clearTimeout(timer);
  }, [link, bacaLink]);

  // ===== JOB =====
  useEffect(() => {
    if (!job?.job_id || selesai(job.status)) return;
    jobRef.current = job.job_id;
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(`/api/video/jobs/${jobRef.current}`, { cache: "no-store" });
        if (res.ok) setJob(await res.json());
      } catch {
        // coba lagi putaran berikutnya
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status]);

  async function mulaiRender() {
    setGalat("");
    setMengirim(true);
    try {
      const texts: Record<string, string> = {};
      const teksHook = susunHook(
        modeTeks === "manual" ? kickerManual : kicker,
        modeTeks === "manual" ? isiManual : isi
      );
      if (teksHook) texts.hook = teksHook;
      // Kredit pemilik video asli, diambil dari link sumber. Huruf kapital
      // supaya seragam dengan tulisan lain di bar merah bawah.
      if (pratinjau?.uploader) {
        texts.sumber = `SUMBER: ${pratinjau.uploader.toUpperCase()}`;
      }
      const res = await fetch("/api/video/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: link.trim(), template_id: ID_LAYER, texts }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal memulai.");
      setJob({ job_id: data.job_id, status: data.status, progress: 0 });
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai.");
    } finally {
      setMengirim(false);
    }
  }

  const sedangJalan = !!job && !selesai(job.status);
  // Hasil yang sudah diunggah ke Supabase dipakai lebih dulu supaya video tetap
  // bisa diputar walau container backend sudah diganti.
  const berkasVideo = job?.url || (job?.output ? `/api/video/jobs/${job.job_id}/file` : "");

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · EDIT VIDEO</span>
        <span className={styles.headStatus}>{job ? job.status : "idle"}</span>
      </header>

      <main className={styles.main}>
        {/* ================= LAYER ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Layer</h2>
            <span className={styles.headStatus}>720 × 1280</span>
          </div>

          <div className={styles.form}>
            <p className={styles.hint}>
              Empat layer dan satu outro, susunannya tetap. Kamu cukup mengganti berkasnya —
              posisi dan waktu tampilnya sudah diatur.
            </p>

            {(susunan?.overlays || []).map((ov, i) => (
              <div key={i} className={styles.row}>
                <label>
                  <span>{NAMA_SLOT[i] || ov.label || `Layer ${i + 1}`}</span>
                  <input
                    readOnly
                    value={ov.file ? ov.file.replace("assets/", "") : "(belum ada berkas)"}
                  />
                </label>
                <label className={styles.uploadBtn}>
                  {ov.file ? "Ganti" : "Unggah"}
                  <input
                    type="file"
                    accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v,.webm"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void gantiLayer(i, f);
                      e.target.value = "";
                    }}
                  />
                </label>
              </div>
            ))}

            <div className={styles.row}>
              <label>
                <span>Outro</span>
                <input
                  readOnly
                  value={susunan?.outro ? susunan.outro.replace("assets/", "") : "(belum ada berkas)"}
                />
              </label>
              <label className={styles.uploadBtn}>
                {susunan?.outro ? "Ganti" : "Unggah"}
                <input
                  type="file"
                  accept=".mp4,.mov,.m4v,.webm"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void gantiOutro(f);
                    e.target.value = "";
                  }}
                />
              </label>
            </div>

            {pesanLayer && <p className={styles.pesan}>{pesanLayer}</p>}
          </div>
        </section>

        {/* ================= BUAT VIDEO ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Buat Video</h2>
          </div>

          <div className={styles.form}>
            <label>
              <span>Link video sumber</span>
              <input
                value={link}
                onChange={(e) => setLink(e.target.value)}
                placeholder="https://www.instagram.com/reel/... atau TikTok / YouTube / link MP4"
              />
            </label>

            {(memuatPratinjau || pratinjau || galatPratinjau) && (
              <div className={styles.pratinjauBox}>
                {memuatPratinjau && <p className={styles.hint}>Membaca isi link ...</p>}
                {galatPratinjau && <p className={styles.error}>{galatPratinjau}</p>}
                {pratinjau && (
                  <>
                    <div className={styles.pratinjauIsi}>
                      {pratinjau.thumbnail && (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          className={styles.pratinjauGambar}
                          src={pratinjau.thumbnail}
                          alt="Sampul video"
                        />
                      )}
                      <div className={styles.pratinjauTeks}>
                        <strong>{pratinjau.title}</strong>
                        <span>
                          {[
                            pratinjau.uploader,
                            pratinjau.duration ? formatDurasi(pratinjau.duration) : "",
                            pratinjau.width ? `${pratinjau.width}×${pratinjau.height}` : "",
                            pratinjau.view_count ? `${formatAngka(pratinjau.view_count)} tayang` : "",
                            pratinjau.like_count ? `${formatAngka(pratinjau.like_count)} suka` : "",
                            formatTanggal(pratinjau.upload_date),
                            pratinjau.extractor,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                        {pratinjau.too_long && (
                          <span className={styles.peringatan}>
                            Lebih panjang dari batas{" "}
                            {Math.round(pratinjau.max_seconds / 60)} menit — hanya bagian awal
                            yang dipakai.
                          </span>
                        )}
                      </div>
                    </div>
                    {pratinjau.description && (
                      <p className={styles.caption}>{pratinjau.description}</p>
                    )}
                  </>
                )}
              </div>
            )}

            <div className={styles.hookBox}>
              <div className={styles.hookHead}>
                <span className={styles.hookJudul}>Teks di video</span>
                <div className={styles.pilihMode}>
                  <button
                    type="button"
                    className={modeTeks === "ai" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => setModeTeks("ai")}
                  >
                    AI
                  </button>
                  <button
                    type="button"
                    className={modeTeks === "manual" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => setModeTeks("manual")}
                  >
                    Manual
                  </button>
                </div>
              </div>

              {modeTeks === "ai" ? (
                <>
                  {membuatHook && <p className={styles.hint}>Menyusun teks dari caption ...</p>}
                  {isi && (
                    <p className={styles.hookParagraf}>
                      <span className={styles.hookKicker}>{kicker || "VIRAL"}!</span> {isi}
                    </p>
                  )}
                  {!membuatHook && !isi && (
                    <p className={styles.hint}>
                      Tempel link video dulu — teksnya disusun dari caption video itu.
                    </p>
                  )}
                  {sumberHook && <p className={styles.hint}>{sumberHook}</p>}
                  {pratinjau && !membuatHook && (
                    <button
                      type="button"
                      className={styles.ghostBtn}
                      onClick={() => void buatHook(pratinjau)}
                    >
                      Buat ulang
                    </button>
                  )}
                </>
              ) : (
                <>
                  <label>
                    <span>Kata pembuka (merah, otomatis diberi tanda seru)</span>
                    <input
                      value={kickerManual}
                      maxLength={12}
                      placeholder={kicker || "VIRAL"}
                      onChange={(e) => setKickerManual(e.target.value)}
                    />
                  </label>
                  <label>
                    <span>Isi berita</span>
                    <textarea
                      className={styles.naskah}
                      rows={3}
                      value={isiManual}
                      maxLength={100}
                      placeholder={isi || "maksimal 100 karakter, dipenggal otomatis jadi maksimal 4 baris"}
                      onChange={(e) => setIsiManual(e.target.value)}
                    />
                  </label>
                  {(kickerManual || isiManual) && (
                    <p className={styles.hookParagraf}>
                      <span className={styles.hookKicker}>
                        {(kickerManual || "VIRAL").replace(/!+$/, "").toUpperCase()}!
                      </span>{" "}
                      {isiManual.toUpperCase()}
                    </p>
                  )}
                  <p className={styles.hint}>
                    Huruf kecil otomatis dijadikan kapital. Kalau terlalu panjang untuk 4 baris,
                    ukuran hurufnya diperkecil sedikit saat dirender.
                  </p>
                  {isi && (
                    <button
                      type="button"
                      className={styles.ghostBtn}
                      onClick={() => {
                        setKickerManual(kicker);
                        setIsiManual(isi);
                      }}
                    >
                      Isi dari saran AI
                    </button>
                  )}
                </>
              )}

              {pratinjau?.uploader && (
                <p className={styles.kredit}>
                  Kredit di bar bawah:{" "}
                  <strong>SUMBER: {pratinjau.uploader.toUpperCase()}</strong>
                </p>
              )}
            </div>

            {galat && <p className={styles.error} role="alert">{galat}</p>}

            <button
              className={styles.mulaiBtn}
              onClick={mulaiRender}
              disabled={mengirim || sedangJalan || !pratinjau}
            >
              {sedangJalan ? "Sedang dibuat..." : "Buat video"}
            </button>

            {job && (
              <div className={styles.jobBox}>
                <div className={styles.jobHead}>
                  <span className={`${styles.badge} ${styles[job.status] || ""}`}>{job.status}</span>
                  <span>{job.message}</span>
                </div>
                <div className={styles.barWrap}>
                  <div className={styles.bar} style={{ width: `${job.progress || 0}%` }} />
                </div>
                <div className={styles.logBody}>
                  {(job.logs || []).map((baris, i) => (
                    <p key={i}>{baris}</p>
                  ))}
                </div>
                {job.status === "done" && berkasVideo && (
                  <div className={styles.hasil}>
                    <video className={styles.video} src={berkasVideo} controls playsInline />
                    <a className={styles.unduhBtn} href={berkasVideo} download>
                      Unduh MP4
                    </a>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
