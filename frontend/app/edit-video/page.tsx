"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";
import { bacaForm, simpanForm } from "../components/formStore";

type Overlay = {
  file: string;
  x: number | null;
  y: number | null;
  w?: number | null;
  h?: number | null;
  crop?: string | null;
  start?: number | null;
  end?: number | null;
  label?: string;
};

type Pratinjau = {
  title: string;
  uploader: string;
  duration: number | null;
  thumbnail: string;
  extractor: string;
  width: number;
  height: number;
  too_long: boolean;
  max_seconds: number;
};

type TextLayer = {
  name: string;
  default?: string;
  size?: number;
  color?: string;
  x?: number | null;
  y?: number | null;
  box?: boolean;
  stroke?: number;
  line_spacing?: number;
  start?: number | null;
  end?: number | null;
};

type Template = {
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

type JobStatus = {
  job_id: string;
  status: string;
  progress: number;
  message?: string;
  logs?: string[];
  output?: string | null;
  error?: string | null;
};

// Batas ukuran satu berkas layer. Standarnya mengikuti backend (200 MB) karena
// auto-comment.tech berjalan sendiri di VPS lewat Docker + Caddy, bukan Vercel.
// Kalau frontend dipasang di Vercel, badan permintaan serverless dibatasi ~4 MB;
// setel NEXT_PUBLIC_MAX_UPLOAD_MB=4 di sana supaya pesannya jelas sejak awal.
const BATAS_UNGGAH_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB || 200);

function apiUrl(path: string): string {
  return path;
}

/** Susunan 4 layer + outro persis seperti skrip main.py (kanvas 720x1280).
 *  Angka posisi diambil dari perintah ffmpeg di skrip itu, jadi hasilnya sama.
 *  Berkas tiap layer dikosongkan — diisi lewat tombol Unggah di barisnya. */
function formatDurasi(detik: number): string {
  const total = Math.round(detik);
  const m = Math.floor(total / 60);
  const d = total % 60;
  return `${m}:${String(d).padStart(2, "0")}`;
}

function templateCapcut(): Template {
  return {
    id: "",
    name: "CapCut AI (4 layer + outro)",
    width: 720,
    height: 1280,
    fps: 30,
    intro: null,
    outro: null,
    max_duration: null,
    overlays: [
      // Urutan array = urutan menumpuk, dari paling bawah ke paling atas.
      { label: "Layer 3 — panel bawah", file: "", x: 0, y: 851, w: 720, h: 429,
        crop: "2880:1197:40:0", start: 0, end: 3 },
      { label: "Layer 2 — kotak lower-third", file: "", x: 10, y: 710, w: 700, h: 283,
        start: 0, end: 3 },
      { label: "Layer 4 — overlay kecil", file: "", x: 45, y: 55, w: 280, h: 158 },
      { label: "Layer 1 — bingkai teratas", file: "", x: 0, y: 63, w: 720, h: 1217 },
    ],
    texts: [
      // Posisi = titik teks di dalam kotak lower-third (10,710) + offset (28,82).
      // start/end wajib sama dengan layer 2 dan 3: teks hook harus ikut hilang
      // saat kotak latarnya hilang, kalau tidak ia melayang di atas video.
      { name: "headline", size: 27, x: 38, y: 792, color: "#d32d27", line_spacing: 26,
        start: 0, end: 3 },
      { name: "detail", size: 27, x: 38, y: 845, color: "black", line_spacing: 26,
        start: 0, end: 3 },
      { name: "message", size: 27, x: 38, y: 898, color: "black", line_spacing: 26,
        start: 0, end: 3 },
    ],
    assets: [],
  };
}

function templateKosong(nama: string): Template {
  return {
    id: "",
    name: nama,
    width: 1080,
    height: 1920,
    fps: 30,
    intro: null,
    outro: null,
    max_duration: null,
    overlays: [],
    texts: [],
    assets: [],
  };
}

const selesai = (status: string) => status === "done" || status === "error";

export default function EditVideoPage() {

  const [templates, setTemplates] = useState<Template[]>([]);
  const [terpilih, setTerpilih] = useState<string>("");
  const [template, setTemplate] = useState<Template | null>(null);
  const [pesanTemplate, setPesanTemplate] = useState("");
  const [menyimpan, setMenyimpan] = useState(false);

  const [link, setLink] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [isiTeks, setIsiTeks] = useState<Record<string, string>>({});
  const [naskah, setNaskah] = useState("");
  const [pratinjau, setPratinjau] = useState<Pratinjau | null>(null);
  const [memuatPratinjau, setMemuatPratinjau] = useState(false);
  const [galatPratinjau, setGalatPratinjau] = useState("");
  const [membuatHook, setMembuatHook] = useState(false);
  const [pesanHook, setPesanHook] = useState("");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [galat, setGalat] = useState("");
  const [mengirim, setMengirim] = useState(false);
  const jobRef = useRef<string>("");

  // ===== MUAT DAFTAR TEMPLATE =====
  const muatTemplates = useCallback(async (pilih?: string) => {
    try {
      const res = await fetch(apiUrl("/api/video/templates"), { cache: "no-store" });
      const data = await res.json();
      const daftar: Template[] = data.templates || [];
      setTemplates(daftar);
      setTerpilih((sekarang) => pilih || sekarang || daftar[0]?.id || "");
    } catch {
      setGalat("Backend tidak merespons. Pastikan servernya berjalan.");
    }
  }, []);

  useEffect(() => {
    // State baru diubah SETELAH fetch selesai (bukan saat efek berjalan),
    // jadi tidak ada render berantai.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void muatTemplates();
  }, [muatTemplates]);

  // ===== ISIAN TERSIMPAN PER AKUN =====
  useEffect(() => {
    const tersimpan = bacaForm("edit-video", { link: "", sessionId: "", template: "" });
    /* eslint-disable react-hooks/set-state-in-effect -- memulihkan isian
       tersimpan hanya sekali saat halaman dibuka. Sengaja dilakukan setelah
       render pertama, bukan lewat nilai awal useState, supaya HTML dari server
       dan dari browser tetap sama (localStorage tidak ada di server). */
    if (tersimpan.link) setLink(tersimpan.link);
    if (tersimpan.sessionId) setSessionId(tersimpan.sessionId);
    if (tersimpan.template) setTerpilih(tersimpan.template);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  // Sama seperti halaman lain: disimpan saat diubah, bukan lewat useEffect.
  function ubahIsian(bagian: { link?: string; sessionId?: string; template?: string }) {
    const berikutnya = { link, sessionId, template: terpilih, ...bagian };
    if (bagian.link !== undefined) setLink(bagian.link);
    if (bagian.sessionId !== undefined) setSessionId(bagian.sessionId);
    if (bagian.template !== undefined) setTerpilih(bagian.template);
    simpanForm("edit-video", berikutnya);
  }

  // ===== MUAT SATU TEMPLATE (termasuk daftar asetnya) =====
  const muatTemplate = useCallback(async (id: string) => {
    // id kosong berarti sedang menyusun template baru yang belum tersimpan —
    // jangan disentuh, kalau tidak formulirnya ikut hilang saat tekan "+ Baru".
    if (!id) return;
    try {
      const res = await fetch(apiUrl(`/api/video/templates/${id}`), { cache: "no-store" });
      if (!res.ok) return;
      const data: Template = await res.json();
      setTemplate(data);
      const awal: Record<string, string> = {};
      (data.texts || []).forEach((t) => (awal[t.name] = t.default || ""));
      setIsiTeks(awal);
    } catch {
      // biarkan; pesan galat sudah ditangani di tempat lain
    }
  }, []);

  useEffect(() => {
    // Sama seperti di atas: perubahan state terjadi setelah fetch selesai.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void muatTemplate(terpilih);
  }, [terpilih, muatTemplate]);

  // ===== PANTAU JOB =====
  useEffect(() => {
    if (!job?.job_id || selesai(job.status)) return;
    jobRef.current = job.job_id;
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(apiUrl(`/api/video/jobs/${jobRef.current}`), { cache: "no-store" });
        if (!res.ok) return;
        setJob(await res.json());
      } catch {
        // coba lagi pada putaran berikutnya
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status]);

  function ubahTemplate(bagian: Partial<Template>) {
    setTemplate((current) => (current ? { ...current, ...bagian } : current));
  }

  async function simpanTemplate() {
    if (!template) return;
    setMenyimpan(true);
    setPesanTemplate("");
    try {
      const id = template.id || template.name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      const res = await fetch(apiUrl(`/api/video/templates?template_id=${encodeURIComponent(id)}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: template.name,
          width: template.width,
          height: template.height,
          fps: template.fps,
          intro: template.intro || null,
          outro: template.outro || null,
          max_duration: template.max_duration || null,
          overlays: template.overlays,
          texts: template.texts,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal menyimpan template.");
      setPesanTemplate("Template tersimpan.");
      await muatTemplates(data.id);
      await muatTemplate(data.id);
    } catch (err) {
      setPesanTemplate(err instanceof Error ? err.message : "Gagal menyimpan template.");
    } finally {
      setMenyimpan(false);
    }
  }

  async function unggahAset(file: File) {
    if (!template?.id) {
      setPesanTemplate("Simpan template dulu sebelum mengunggah aset.");
      return;
    }
    if (file.size > BATAS_UNGGAH_MB * 1_048_576) {
      setPesanTemplate(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar lewat website ` +
          `(batas ${BATAS_UNGGAH_MB} MB).`
      );
      return;
    }
    setPesanTemplate(`Mengunggah ${file.name} ...`);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(apiUrl(`/api/video/templates/${template.id}/assets`), {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal mengunggah.");
      setPesanTemplate(`${data.file} terunggah.`);
      await muatTemplate(template.id);
    } catch (err) {
      setPesanTemplate(err instanceof Error ? err.message : "Gagal mengunggah.");
    }
  }

  /** Unggah beberapa berkas layer berurutan, satu per satu.
   *  Sengaja tidak paralel supaya progresnya terbaca dan server tidak dibanjiri. */
  async function unggahBanyakAset(daftar: File[]) {
    for (let i = 0; i < daftar.length; i += 1) {
      setPesanTemplate(`Mengunggah ${i + 1}/${daftar.length}: ${daftar[i].name} ...`);
      await unggahAset(daftar[i]);
    }
    setPesanTemplate(`Selesai mengunggah ${daftar.length} berkas.`);
  }

  /** Minta backend merapikan naskah jadi 3 baris lower-third, lalu isikan ke
   *  layer teks bernama headline/detail/message. Kalau template memakai nama
   *  lain, tiga layer teks pertama yang diisi. */
  async function buatHookOtomatis() {
    if (naskah.trim().length < 3) {
      setPesanHook("Tulis dulu naskah singkat tentang isi videonya.");
      return;
    }
    setMembuatHook(true);
    setPesanHook("Membuat hook ...");
    try {
      const res = await fetch(apiUrl("/api/video/hook"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ naskah }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal membuat hook.");

      const baris: string[] = data.lines || [];
      const namaLayer = (template?.texts || []).map((t) => t.name);
      const bawaan = ["headline", "detail", "message"];
      // Pakai nama baku kalau template memang punya; kalau tidak, urutan apa adanya.
      const sasaran = bawaan.every((n) => namaLayer.includes(n))
        ? bawaan
        : namaLayer.slice(0, baris.length);

      const isian: Record<string, string> = { ...isiTeks };
      sasaran.forEach((nama, i) => {
        if (baris[i] !== undefined) isian[nama] = baris[i];
      });
      setIsiTeks(isian);
      setPesanHook(
        data.source === "deepseek"
          ? "Hook dibuat oleh AI. Silakan sunting kalau perlu."
          : "DeepSeek tidak aktif, dipakai ringkasan sederhana. Silakan sunting."
      );
    } catch (err) {
      setPesanHook(err instanceof Error ? err.message : "Gagal membuat hook.");
    } finally {
      setMembuatHook(false);
    }
  }

  /** Unggah berkas lalu kembalikan nama asetnya ("assets/xxx.png"). */
  async function unggahDanAmbilNama(file: File): Promise<string | null> {
    if (!template?.id) {
      setPesanTemplate("Simpan template dulu sebelum mengunggah berkas layer.");
      return null;
    }
    if (file.size > BATAS_UNGGAH_MB * 1_048_576) {
      setPesanTemplate(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar (batas ${BATAS_UNGGAH_MB} MB).`
      );
      return null;
    }
    setPesanTemplate(`Mengunggah ${file.name} ...`);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(apiUrl(`/api/video/templates/${template.id}/assets`), {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal mengunggah.");
      setPesanTemplate(`${data.file} terunggah.`);
      return data.file as string;
    } catch (err) {
      setPesanTemplate(err instanceof Error ? err.message : "Gagal mengunggah.");
      return null;
    }
  }

  /** Unggah berkas untuk satu baris layer, lalu pasang ke layer itu juga. */
  async function unggahUntukLayer(index: number, file: File) {
    const nama = await unggahDanAmbilNama(file);
    if (!nama || !template) return;
    const baru = [...template.overlays];
    baru[index] = { ...baru[index], file: nama };
    ubahTemplate({ overlays: baru });
    if (template.id) await muatTemplate(template.id);
  }

  /** Unggah berkas untuk intro atau outro sekaligus memasangnya. */
  async function unggahUntukKlip(posisi: "intro" | "outro", file: File) {
    const nama = await unggahDanAmbilNama(file);
    if (!nama) return;
    ubahTemplate({ [posisi]: nama } as Partial<Template>);
    if (template?.id) await muatTemplate(template.id);
  }

  /** Baca isi link (judul, durasi, sampul) tanpa mengunduh videonya. */
  const bacaPratinjau = useCallback(
    async (tautan: string, sesi: string) => {
      if (!tautan.toLowerCase().startsWith("http")) {
        setPratinjau(null);
        setGalatPratinjau("");
        return;
      }
      setMemuatPratinjau(true);
      setGalatPratinjau("");
      try {
        const res = await fetch(apiUrl("/api/video/preview"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: tautan, session_id: sesi }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Tidak bisa membaca link.");
        setPratinjau(data as Pratinjau);
      } catch (err) {
        setPratinjau(null);
        setGalatPratinjau(err instanceof Error ? err.message : "Tidak bisa membaca link.");
      } finally {
        setMemuatPratinjau(false);
      }
    },
    []
  );

  // Baca link otomatis, ditunda sebentar supaya tidak memanggil server
  // setiap kali satu huruf diketik.
  useEffect(() => {
    // Pembersihan state ditangani di dalam bacaPratinjau (link kosong atau
    // bukan http akan mengosongkan kartu), supaya effect ini tidak memanggil
    // setState secara langsung.
    const timer = setTimeout(() => void bacaPratinjau(link.trim(), sessionId), 700);
    return () => clearTimeout(timer);
  }, [link, sessionId, bacaPratinjau]);

  async function mulaiRender() {
    setGalat("");
    if (!terpilih) return setGalat("Pilih template dulu.");
    if (!link.trim()) return setGalat("Tempel link videonya dulu.");
    setMengirim(true);
    try {
      const res = await fetch(apiUrl("/api/video/jobs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: link.trim(),
          template_id: terpilih,
          texts: isiTeks,
          session_id: sessionId.trim(),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal memulai.");
      setJob({ job_id: data.job_id, status: "queued", progress: 0, logs: [] });
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai.");
    } finally {
      setMengirim(false);
    }
  }

  const berkasVideo = job?.output ? apiUrl(`/api/video/jobs/${job.job_id}/file`) : "";
  const aset = template?.assets || [];
  const asetVideo = aset.filter((a) => /\.(mp4|mov|m4v|webm)$/i.test(a));
  const asetGambar = aset.filter((a) => /\.(png|jpe?g|webp)$/i.test(a));

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · EDIT VIDEO</span>
        <span className={styles.headStatus}>{job ? job.status : "idle"}</span>
      </header>

      <main className={styles.main}>
        {/* ================= TEMPLATE ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Template Layer</h2>
            <div className={styles.headActions}>
              <select
                className={styles.select}
                value={terpilih}
                onChange={(e) => ubahIsian({ template: e.target.value })}
              >
                {templates.length === 0 && <option value="">(belum ada template)</option>}
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={() => {
                  setTemplate(templateKosong("Template Baru"));
                  setTerpilih("");
                }}
              >
                + Baru
              </button>
              <button
                type="button"
                className={styles.ghostBtn}
                title="Susunan 4 layer + outro seperti skrip main.py"
                onClick={() => {
                  setTemplate(templateCapcut());
                  setTerpilih("");
                }}
              >
                + 4 Layer + Outro
              </button>
            </div>
          </div>

          {!template ? (
            <p className={styles.hint}>Pilih template di atas, atau buat baru.</p>
          ) : (
            <div className={styles.form}>
              <div className={styles.row}>
                <label>
                  <span>Nama template</span>
                  <input
                    value={template.name}
                    onChange={(e) => ubahTemplate({ name: e.target.value })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>Lebar</span>
                  <input
                    type="number"
                    value={template.width}
                    onChange={(e) => ubahTemplate({ width: Number(e.target.value) })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>Tinggi</span>
                  <input
                    type="number"
                    value={template.height}
                    onChange={(e) => ubahTemplate({ height: Number(e.target.value) })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>FPS</span>
                  <input
                    type="number"
                    value={template.fps}
                    onChange={(e) => ubahTemplate({ fps: Number(e.target.value) })}
                  />
                </label>
              </div>

              {/* ---- ASET ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Aset layer</h3>
                  <label className={styles.uploadBtn}>
                    + Unggah berkas
                    <input
                      type="file"
                      multiple
                      accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v,.webm"
                      onChange={(e) => {
                        const daftar = Array.from(e.target.files || []);
                        if (daftar.length) void unggahBanyakAset(daftar);
                        e.target.value = "";
                      }}
                    />
                  </label>
                </div>
                {aset.length === 0 ? (
                  <p className={styles.hint}>
                    Belum ada aset. Unggah PNG untuk overlay, dan MP4 untuk intro/outro.
                  </p>
                ) : (
                  <ul className={styles.asetList}>
                    {aset.map((a) => (
                      <li key={a}><code>{a}</code></li>
                    ))}
                  </ul>
                )}
              </div>

              {/* ---- INTRO / OUTRO ---- */}
              <div className={styles.row}>
                <label>
                  <span>Intro</span>
                  <select
                    value={template.intro || ""}
                    onChange={(e) => ubahTemplate({ intro: e.target.value || null })}
                  >
                    <option value="">(tanpa intro)</option>
                    {asetVideo.map((a) => (
                      <option key={a} value={`assets/${a}`}>{a}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Outro</span>
                  <select
                    value={template.outro || ""}
                    onChange={(e) => ubahTemplate({ outro: e.target.value || null })}
                  >
                    <option value="">(tanpa outro)</option>
                    {asetVideo.map((a) => (
                      <option key={a} value={`assets/${a}`}>{a}</option>
                    ))}
                  </select>
                </label>
                <label className={styles.uploadBtn}>
                  Unggah outro
                  <input
                    type="file"
                    accept=".mp4,.mov,.m4v,.webm"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void unggahUntukKlip("outro", f);
                      e.target.value = "";
                    }}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>Durasi maks (detik)</span>
                  <input
                    type="number"
                    value={template.max_duration ?? ""}
                    placeholder="penuh"
                    onChange={(e) =>
                      ubahTemplate({ max_duration: e.target.value ? Number(e.target.value) : null })
                    }
                  />
                </label>
              </div>

              {/* ---- OVERLAY ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Overlay gambar</h3>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    disabled={asetGambar.length === 0}
                    onClick={() =>
                      ubahTemplate({
                        overlays: [
                          ...template.overlays,
                          { file: `assets/${asetGambar[0]}`, x: 0, y: 0 },
                        ],
                      })
                    }
                  >
                    + Tambah
                  </button>
                </div>
                {template.overlays.map((ov, i) => (
                  <div key={i} className={styles.row}>
                    <label>
                      <span>{ov.label || `Layer ${i + 1}`}</span>
                      <select
                        value={ov.file}
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, file: e.target.value };
                          ubahTemplate({ overlays: baru });
                        }}
                      >
                        <option value="">(belum ada berkas)</option>
                        {aset.map((a) => (
                          <option key={a} value={`assets/${a}`}>{a}</option>
                        ))}
                      </select>
                    </label>
                    <label className={styles.uploadBtn}>
                      Unggah
                      <input
                        type="file"
                        accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v,.webm"
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) void unggahUntukLayer(i, f);
                          e.target.value = "";
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>X</span>
                      <input
                        type="number"
                        value={ov.x ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, x: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ overlays: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Y</span>
                      <input
                        type="number"
                        value={ov.y ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, y: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ overlays: baru });
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      className={styles.hapusBtn}
                      onClick={() =>
                        ubahTemplate({ overlays: template.overlays.filter((_, j) => j !== i) })
                      }
                    >
                      Hapus
                    </button>
                  </div>
                ))}
              </div>

              {/* ---- TEKS ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Teks dinamis</h3>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={() =>
                      ubahTemplate({
                        texts: [
                          ...template.texts,
                          { name: `teks${template.texts.length + 1}`, size: 64, y: 150, color: "white" },
                        ],
                      })
                    }
                  >
                    + Tambah
                  </button>
                </div>
                {template.texts.map((t, i) => (
                  <div key={i} className={styles.row}>
                    <label>
                      <span>Nama isian</span>
                      <input
                        value={t.name}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, name: e.target.value };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Ukuran</span>
                      <input
                        type="number"
                        value={t.size ?? 64}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, size: Number(e.target.value) };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Y</span>
                      <input
                        type="number"
                        value={t.y ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, y: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Warna</span>
                      <input
                        value={t.color ?? "white"}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, color: e.target.value };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      className={styles.hapusBtn}
                      onClick={() => ubahTemplate({ texts: template.texts.filter((_, j) => j !== i) })}
                    >
                      Hapus
                    </button>
                  </div>
                ))}
              </div>

              <div className={styles.aksi}>
                <button className={styles.simpanBtn} onClick={simpanTemplate} disabled={menyimpan}>
                  {menyimpan ? "Menyimpan..." : "Simpan template"}
                </button>
                {pesanTemplate && <span className={styles.pesan}>{pesanTemplate}</span>}
              </div>
            </div>
          )}
        </section>

        {/* ================= RENDER ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Buat Video</h2>
          </div>
          <div className={styles.form}>
            <label>
              <span>Link video sumber</span>
              <input
                value={link}
                onChange={(e) => ubahIsian({ link: e.target.value })}
                placeholder="https://www.instagram.com/reel/... atau TikTok / YouTube / link MP4"
              />
            </label>
            {link.toLowerCase().includes("instagram.com") && (
              <label>
                <span>Session ID (untuk link Instagram)</span>
                <input
                  value={sessionId}
                  onChange={(e) => ubahIsian({ sessionId: e.target.value })}
                  placeholder="tempel sessionid cookie"
                  autoComplete="off"
                />
              </label>
            )}

            {(memuatPratinjau || pratinjau || galatPratinjau) && (
              <div className={styles.pratinjauBox}>
                {memuatPratinjau && <p className={styles.hint}>Membaca isi link ...</p>}
                {galatPratinjau && <p className={styles.error}>{galatPratinjau}</p>}
                {pratinjau && (
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
                          pratinjau.width ? `${pratinjau.width}x${pratinjau.height}` : "",
                          pratinjau.extractor,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                      {pratinjau.too_long && (
                        <span className={styles.peringatan}>
                          Video lebih panjang dari batas {Math.round(pratinjau.max_seconds / 60)} menit —
                          hanya bagian awalnya yang dipakai.
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}

            {(template?.texts || []).length > 0 && (
              <div className={styles.hookBox}>
                <label>
                  <span>Naskah singkat (untuk hook otomatis)</span>
                  <textarea
                    className={styles.naskah}
                    rows={3}
                    value={naskah}
                    onChange={(e) => setNaskah(e.target.value)}
                    placeholder="Ceritakan isi videonya beberapa kalimat. AI merapikannya jadi 3 baris gaya berita."
                  />
                </label>
                <button
                  type="button"
                  className={styles.hookBtn}
                  onClick={buatHookOtomatis}
                  disabled={membuatHook}
                >
                  {membuatHook ? "Membuat hook..." : "Buatkan hook otomatis"}
                </button>
                {pesanHook && <p className={styles.hint}>{pesanHook}</p>}
              </div>
            )}

            {(template?.texts || []).map((t) => (
              <label key={t.name}>
                <span>Teks: {t.name}</span>
                <input
                  value={isiTeks[t.name] ?? ""}
                  onChange={(e) => setIsiTeks({ ...isiTeks, [t.name]: e.target.value })}
                  placeholder={t.default || "(kosongkan untuk melewati)"}
                />
              </label>
            ))}

            {galat && <p className={styles.error} role="alert">{galat}</p>}

            <button
              className={styles.mulaiBtn}
              onClick={mulaiRender}
              disabled={mengirim || (!!job && !selesai(job.status))}
            >
              {job && !selesai(job.status) ? "Sedang dibuat..." : "Buat video"}
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
