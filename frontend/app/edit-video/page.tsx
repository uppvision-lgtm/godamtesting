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

// Set layer pertama. Id "utama" dipertahankan dari versi sebelumnya supaya
// berkas yang sudah diunggah tidak perlu diunggah ulang; namanya kini "TV
// Rakyat". Set berikutnya dibuat lewat tombol "Baru" dengan id sendiri.
const ID_SET_PERTAMA = "utama";
const NAMA_SET_PERTAMA = "TV Rakyat";

const BATAS_UNGGAH_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB || 200);

type Overlay = {
  file: string;
  // Boleh angka atau rumus ffmpeg seperti "main_h-h" (tempel ke dasar layar).
  x: number | string | null;
  y: number | string | null;
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
  // Isi diambil dari field set layer (mis. "kategori"), bukan dari job.
  source?: string;
};

/** Kotak tempat teks hook diletakkan, dalam piksel kanvas. */
type KotakTeks = { x: number; y: number; w: number; h: number };

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
  text_box?: KotakTeks | null;
  badge_box?: KotakTeks | null;
  badge_box_default?: KotakTeks | null;
  kategori?: string;
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
  template_id?: string;
};

/** Nama slot yang tampil di layar, berurutan dari lapisan bawah ke atas.
 *  Nama diambil dari sini, bukan dari data tersimpan, supaya penggantian nama
 *  langsung terlihat tanpa perlu membuat ulang susunan layernya. */
const NAMA_SLOT = [
  "kotak monas",
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

/** Badge kategori (NEWS / HIBURAN / SHOWBIZ). Isinya dari field "kategori"
 *  set layer, posisinya dari badge_box set itu (atau tebakan di atas kotak
 *  teks kalau belum digambar). */
const TEKS_KATEGORI: TextLayer = {
  name: "kategori",
  style: "kategori",
  source: "kategori",
  color: "white",
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

function susunanBawaan(id: string, nama: string): Susunan {
  return {
    id,
    name: nama,
    width: 720,
    height: 1280,
    fps: 30,
    intro: null,
    outro: null,
    max_duration: null,
    overlays: [
      // Panel bawah: selebar layar penuh, tidak ada yang terpotong kiri-kanan.
      // Tingginya sengaja dikosongkan supaya dihitung sendiri dari rasio
      // berkasnya, dan "main_h-h" menempelkannya ke dasar layar. Dengan
      // begitu berkas desain apa pun tampil utuh dan tidak gepeng, tanpa
      // perlu menyetel ulang angkanya tiap kali berkasnya diganti.
      { label: NAMA_SLOT[0], file: "", x: 0, y: "main_h-h", w: 720, h: null,
        start: 0, end: 3 },
      // loop: berkasnya berupa video pendek. Tanpa diulang, animasinya
      // membeku di frame terakhir begitu videonya habis. Pengulangan hanya
      // berlaku di badan video — outro disusun terpisah, jadi layer ini
      // tidak pernah muncul di sana.
      { label: NAMA_SLOT[1], file: "", x: 45, y: 55, w: 280, h: 158, loop: true },
      // Bingkai juga selebar layar dan menempel ke dasar, tingginya mengikuti
      // rasio berkasnya. Pinggiran transparan sudah dipangkas saat diunggah,
      // jadi isinya benar-benar rapat ke dasar dan tidak menyisakan celah
      // yang membuat video sumber mengintip di baris terbawah.
      { label: NAMA_SLOT[2], file: "", x: 0, y: "main_h-h", w: 720, h: null },
    ],
    texts: [TEKS_HOOK, TEKS_KATEGORI, TEKS_SUMBER],
    text_box: null,
    badge_box: null,
    kategori: "",
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
  const [daftarSet, setDaftarSet] = useState<{ id: string; name: string }[]>([]);
  const [idSet, setIdSet] = useState(ID_SET_PERTAMA);
  const [namaSet, setNamaSet] = useState(NAMA_SET_PERTAMA);
  const [menyimpanNama, setMenyimpanNama] = useState(false);
  const [pesanLayer, setPesanLayer] = useState("");
  // Kotak teks set yang sedang dibuka, plus keadaan saat pengguna menggambar.
  const [kotakTeks, setKotakTeks] = useState<KotakTeks | null>(null);
  const [kotakSementara, setKotakSementara] = useState<KotakTeks | null>(null);
  const seretRef = useRef<{ x: number; y: number } | null>(null);
  const pratinjauRef = useRef<HTMLDivElement>(null);
  const [versiPratinjau, setVersiPratinjau] = useState(0);
  const [mendeteksi, setMendeteksi] = useState(false);
  const [kategori, setKategori] = useState("");
  const [badgeBox, setBadgeBox] = useState<KotakTeks | null>(null);
  const [badgeBoxBawaan, setBadgeBoxBawaan] = useState<KotakTeks | null>(null);
  // Kotak mana yang sedang digambar di pratinjau.
  const [modeGambar, setModeGambar] = useState<"teks" | "kategori">("teks");
  // Set layer yang dicentang untuk dirender sekaligus.
  const [pilihanSet, setPilihanSet] = useState<string[]>([]);

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

  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");

  useEffect(() => {
    simpanForm("edit-video", { link });
  }, [link]);

  // ===== SET LAYER =====

  /** Ambil daftar semua set layer yang tersimpan. */
  const muatDaftar = useCallback(async () => {
    try {
      const res = await fetch("/api/video/templates", { cache: "no-store" });
      if (!res.ok) return [];
      const data = await res.json();
      const daftar = (data.templates || []).map((t: Susunan) => ({ id: t.id, name: t.name }));
      setDaftarSet(daftar);
      // Buang centang untuk set yang sudah tidak ada; kalau jadi kosong,
      // centang set pertama supaya tombol Buat video selalu punya sasaran.
      setPilihanSet((lama) => {
        const sah = lama.filter((id) => daftar.some((d: { id: string }) => d.id === id));
        return sah.length ? sah : daftar[0] ? [daftar[0].id] : [];
      });
      return daftar;
    } catch {
      return [];
    }
  }, []);

  /** Muat satu set layer. Kalau belum ada, dibuatkan dengan susunan bawaan. */
  const muatSet = useCallback(async (id: string, nama: string) => {
    try {
      const res = await fetch(`/api/video/templates/${id}`, { cache: "no-store" });
      if (res.ok) {
        const ada: Susunan = await res.json();
        // Layer teks selalu didefinisikan di kode (hook + kredit sumber), jadi
        // set tersimpan disamakan dengannya. Dengan begini set lama otomatis
        // ikut ke bentuk terbaru tanpa perlu dibuat ulang dan tanpa mengunggah
        // lagi berkas layernya.
        const tetap = [TEKS_HOOK, TEKS_KATEGORI, TEKS_SUMBER];

        // Slot layer juga disamakan dengan susunan di kode, dan hanya berkas
        // pilihan pengguna yang dipertahankan. Set lama punya satu slot lebih
        // banyak ("kotak putih 3 detik awal" di urutan kedua) yang kini sudah
        // dihapus; slot itu dibuang lebih dulu supaya berkas layer sesudahnya
        // tidak bergeser ke slot yang salah.
        const acuan = susunanBawaan(ada.id, ada.name).overlays;
        let lama = ada.overlays || [];
        if (lama.length === acuan.length + 1) {
          lama = lama.filter((_, i) => i !== 1);
        }
        const slot = acuan.map((a, i) => ({ ...a, file: lama[i]?.file || "" }));

        const perluDisimpan =
          JSON.stringify(ada.texts || []) !== JSON.stringify(tetap) ||
          JSON.stringify(ada.overlays || []) !== JSON.stringify(slot);
        ada.texts = tetap;
        ada.overlays = slot;
        setIdSet(ada.id);
        setNamaSet(ada.name);
        setKotakTeks(ada.text_box || null);
        setBadgeBox(ada.badge_box || null);
        setBadgeBoxBawaan(ada.badge_box_default || null);
        setKategori(ada.kategori || "");
        if (perluDisimpan) {
          await simpanSusunan(ada, ada.id);
        } else {
          setSusunan(ada);
        }
        return;
      }
    } catch {
      // lanjut ke pembuatan di bawah
    }
    try {
      const res = await fetch(`/api/video/templates?template_id=${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(susunanBawaan(id, nama)),
      });
      if (res.ok) {
        const dibuat = await res.json();
        setSusunan(dibuat);
        setIdSet(dibuat.id);
        setNamaSet(dibuat.name);
        setKotakTeks(null);
        setBadgeBox(null);
        setBadgeBoxBawaan(null);
        setKategori("");
        await muatDaftar();
      }
    } catch {
      setPesanLayer("Tidak bisa menyiapkan set layer.");
    }
    // simpanSusunan sengaja tidak masuk daftar dependensi: ia didefinisikan
    // ulang setiap render, jadi memasukkannya membuat muatSet ikut berubah
    // terus dan effect di bawah berjalan berulang tanpa henti.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [muatDaftar]);

  useEffect(() => {
    void (async () => {
      const daftar = await muatDaftar();
      const pertama = daftar[0];
      await muatSet(pertama?.id || ID_SET_PERTAMA, pertama?.name || NAMA_SET_PERTAMA);
    })();
  }, [muatDaftar, muatSet]);

  /** Simpan set layer (dipanggil setelah berkas diganti atau nama diubah). */
  async function simpanSusunan(baru: Susunan, id?: string) {
    const sasaran = id || idSet;
    setSusunan(baru);
    await fetch(`/api/video/templates?template_id=${sasaran}`, {
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
        text_box: baru.text_box ?? null,
        badge_box: baru.badge_box ?? null,
        kategori: baru.kategori ?? "",
      }),
    });
    // Pratinjau digambar dari layer tersimpan, jadi dimuat ulang tiap ada perubahan.
    setVersiPratinjau((v) => v + 1);
    // Tebakan kotak badge dihitung server dari kotak teks; ambil yang terbaru.
    try {
      const r = await fetch(`/api/video/templates/${sasaran}`, { cache: "no-store" });
      if (r.ok) {
        const d: Susunan = await r.json();
        setBadgeBoxBawaan(d.badge_box_default || null);
      }
    } catch {
      // tebakan lama tetap dipakai
    }
  }

  /** Hapus set layer yang sedang dibuka beserta berkasnya. */
  async function hapusSet() {
    if (!susunan) return;
    const nama = namaSet;
    if (!window.confirm(`Hapus set layer "${nama}"? Berkas layer di dalamnya ikut terhapus.`)) return;
    try {
      const res = await fetch(`/api/video/templates/${idSet}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Gagal menghapus set layer.");
      const daftar = await muatDaftar();
      const berikut = daftar[0];
      await muatSet(berikut?.id || ID_SET_PERTAMA, berikut?.name || NAMA_SET_PERTAMA);
      setPesanLayer(`Set layer "${nama}" dihapus.`);
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Gagal menghapus set layer.");
    }
  }

  /** Simpan teks kategori (NEWS / HIBURAN / SHOWBIZ) milik set ini. */
  async function simpanKategori() {
    if (!susunan) return;
    const bersih = kategori.trim().toUpperCase();
    if (bersih === (susunan.kategori || "")) return;
    setKategori(bersih);
    await simpanSusunan({ ...susunan, kategori: bersih });
    setPesanLayer(bersih ? `Kategori "${bersih}" tersimpan.` : "Kategori dikosongkan; badge tidak ditulis.");
  }

  async function simpanBadge(kotak: KotakTeks | null) {
    if (!susunan) return;
    setBadgeBox(kotak);
    await simpanSusunan({ ...susunan, badge_box: kotak });
    setPesanLayer(kotak ? "Kotak kategori tersimpan." : "Kotak kategori dihapus; dipakai tebakan di atas kotak teks.");
  }

  function gayaKotak(k: KotakTeks): React.CSSProperties {
    const lebar = susunan?.width || 720;
    const tinggi = susunan?.height || 1280;
    return {
      left: `${(k.x / lebar) * 100}%`,
      top: `${(k.y / tinggi) * 100}%`,
      width: `${(k.w / lebar) * 100}%`,
      height: `${(k.h / tinggi) * 100}%`,
    };
  }

  // ===== KOTAK TEKS =====

  async function simpanKotak(kotak: KotakTeks | null) {
    if (!susunan) return;
    setKotakTeks(kotak);
    await simpanSusunan({ ...susunan, text_box: kotak });
    setPesanLayer(kotak ? "Kotak teks tersimpan." : "Kotak teks dihapus; teks memakai posisi bawaan.");
  }

  /** Minta server menebak kotak teks dari gambar layer. */
  async function deteksiKotak() {
    setMendeteksi(true);
    setPesanLayer("Mencari kotak teks di layer ...");
    try {
      const res = await fetch(`/api/video/templates/${idSet}/detect-box`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Tidak menemukan kotak teks.");
      await simpanKotak(data.text_box as KotakTeks);
      setPesanLayer("Kotak teks ditemukan. Kalau meleset, gambar ulang dengan menyeret di pratinjau.");
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Tidak menemukan kotak teks.");
    } finally {
      setMendeteksi(false);
    }
  }

  /** Ubah posisi pointer di pratinjau jadi koordinat kanvas. */
  function titikKanvas(e: React.PointerEvent<HTMLDivElement>): { x: number; y: number } {
    const el = pratinjauRef.current;
    const lebar = susunan?.width || 720;
    const tinggi = susunan?.height || 1280;
    if (!el) return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();
    const x = Math.round(((e.clientX - r.left) / r.width) * lebar);
    const y = Math.round(((e.clientY - r.top) / r.height) * tinggi);
    return { x: Math.max(0, Math.min(lebar, x)), y: Math.max(0, Math.min(tinggi, y)) };
  }

  function mulaiSeret(e: React.PointerEvent<HTMLDivElement>) {
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = titikKanvas(e);
    seretRef.current = p;
    setKotakSementara({ x: p.x, y: p.y, w: 0, h: 0 });
  }

  function gerakSeret(e: React.PointerEvent<HTMLDivElement>) {
    const awal = seretRef.current;
    if (!awal) return;
    const p = titikKanvas(e);
    setKotakSementara({
      x: Math.min(awal.x, p.x),
      y: Math.min(awal.y, p.y),
      w: Math.abs(p.x - awal.x),
      h: Math.abs(p.y - awal.y),
    });
  }

  function selesaiSeret() {
    const kotak = kotakSementara;
    seretRef.current = null;
    setKotakSementara(null);
    // Seretan terlalu kecil dianggap klik tak sengaja.
    if (!kotak || kotak.w < 20 || kotak.h < 20) return;
    if (modeGambar === "kategori") void simpanBadge(kotak);
    else void simpanKotak(kotak);
  }

  /** Simpan nama set yang sedang dibuka. */
  async function simpanNama() {
    if (!susunan) return;
    const nama = namaSet.trim();
    if (!nama) {
      setPesanLayer("Nama set layer tidak boleh kosong.");
      return;
    }
    setMenyimpanNama(true);
    setPesanLayer("Menyimpan ...");
    try {
      await simpanSusunan({ ...susunan, name: nama });
      await muatDaftar();
      setPesanLayer(`Set layer "${nama}" tersimpan.`);
    } catch {
      setPesanLayer("Gagal menyimpan set layer.");
    } finally {
      setMenyimpanNama(false);
    }
  }

  /** Buat set layer baru yang kosong. */
  async function buatSetBaru() {
    const nama = (window.prompt("Nama set layer baru:", "") || "").trim();
    if (!nama) return;
    // Id dibuat dari namanya supaya mudah dikenali di penyimpanan, dengan
    // akhiran acak agar dua set bernama mirip tidak saling menimpa.
    const id =
      nama.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) +
      "-" +
      Math.random().toString(36).slice(2, 6);
    await muatSet(id, nama);
    setPesanLayer(`Set layer "${nama}" dibuat. Unggah berkas tiap layernya.`);
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
      const res = await fetch(`/api/video/templates/${idSet}/assets`, {
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

  /** Kosongkan satu slot layer. Berkasnya tetap tersimpan di set ini, hanya
   *  tidak dipakai lagi — layer tanpa berkas dilewati saat render. */
  async function kosongkanLayer(index: number) {
    if (!susunan) return;
    const overlays = [...susunan.overlays];
    overlays[index] = { ...overlays[index], file: "" };
    await simpanSusunan({ ...susunan, overlays });
    setPesanLayer(`${NAMA_SLOT[index] || `Layer ${index + 1}`} dikosongkan.`);
  }

  /** Kosongkan outro. */
  async function kosongkanOutro() {
    if (!susunan) return;
    await simpanSusunan({ ...susunan, outro: null });
    setPesanLayer("Outro dikosongkan.");
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
  // Semua job yang belum selesai disegarkan bersama dalam satu putaran.
  const kunciAktif = jobs.filter((j) => !selesai(j.status)).map((j) => j.job_id).join(",");
  useEffect(() => {
    const aktif = kunciAktif ? kunciAktif.split(",") : [];
    if (aktif.length === 0) return;
    const timer = window.setInterval(async () => {
      const hasil = await Promise.all(
        aktif.map(async (id) => {
          try {
            const r = await fetch(`/api/video/jobs/${id}`, { cache: "no-store" });
            return r.ok ? ((await r.json()) as JobStatus) : null;
          } catch {
            return null;
          }
        })
      );
      setJobs((lama) => lama.map((j) => hasil.find((h) => h && h.job_id === j.job_id) || j));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [kunciAktif]);

  function namaSetDari(id?: string): string {
    return daftarSet.find((d) => d.id === id)?.name || id || "";
  }

  /** Alamat video hasil: dari Supabase kalau sudah tersimpan, kalau tidak dari server. */
  function berkasJob(j: JobStatus): string {
    if (j.status !== "done") return "";
    if (j.url && /\/jobs\/[^/]+\/output\.mp4/.test(j.url)) return j.url;
    return j.output ? `/api/video/jobs/${j.job_id}/file` : "";
  }

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
      const sasaran = pilihanSet.length ? pilihanSet : [idSet];
      const res = await fetch("/api/video/jobs/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: link.trim(), template_ids: sasaran, texts }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal memulai.");
      const baru: JobStatus[] = (data.jobs || []).map(
        (j: { job_id: string; template_id: string; status: string }) => ({
          job_id: j.job_id,
          status: j.status,
          progress: 0,
          template_id: j.template_id,
        })
      );
      // Daftar diganti, bukan ditambah: yang tampil di bawah selalu persis
      // template yang baru saja dicentang. Hasil lama tetap tersimpan di
      // server dan Supabase, hanya tidak ikut ditampilkan lagi.
      setJobs(baru);
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai.");
    } finally {
      setMengirim(false);
    }
  }

  const sedangJalan = jobs.some((j) => !selesai(j.status));

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · EDIT VIDEO</span>
        <span className={styles.headStatus}>{sedangJalan ? "rendering" : "idle"}</span>
      </header>

      <main className={styles.main}>
        {/* ================= LAYER ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Layer</h2>
            <span className={styles.headStatus}>720 × 1280</span>
          </div>

          <div className={styles.form}>
            <div className={styles.row}>
              <label>
                <span>Set layer</span>
                <select
                  className={styles.select}
                  value={idSet}
                  onChange={(e) => {
                    const dipilih = daftarSet.find((d) => d.id === e.target.value);
                    if (dipilih) void muatSet(dipilih.id, dipilih.name);
                  }}
                >
                  {daftarSet.length === 0 && <option value={idSet}>{namaSet}</option>}
                  {daftarSet.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              </label>
              <button type="button" className={styles.ghostBtn} onClick={buatSetBaru}>
                + Baru
              </button>
              <button
                type="button"
                className={styles.hapusBtn}
                onClick={hapusSet}
                disabled={!susunan}
                title="Hapus set layer yang sedang dibuka"
              >
                Hapus
              </button>
            </div>

            <div className={styles.row}>
              <label>
                <span>Nama set</span>
                <input
                  value={namaSet}
                  maxLength={60}
                  onChange={(e) => setNamaSet(e.target.value)}
                  placeholder="mis. TV Rakyat"
                />
              </label>
              <button
                type="button"
                className={styles.simpanBtn}
                onClick={simpanNama}
                disabled={menyimpanNama}
              >
                {menyimpanNama ? "Menyimpan..." : "Simpan"}
              </button>
            </div>

            <p className={styles.hint}>
              Empat layer dan satu outro, susunannya tetap. Kamu cukup mengganti berkasnya —
              posisi dan waktu tampilnya sudah diatur. Berkas tersimpan per set layer.
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
                {ov.file && (
                  <button
                    type="button"
                    className={styles.kosongkanBtn}
                    title={`Kosongkan ${NAMA_SLOT[i] || `layer ${i + 1}`}`}
                    aria-label={`Kosongkan ${NAMA_SLOT[i] || `layer ${i + 1}`}`}
                    onClick={() => void kosongkanLayer(i)}
                  >
                    ×
                  </button>
                )}
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
              {susunan?.outro && (
                <button
                  type="button"
                  className={styles.kosongkanBtn}
                  title="Kosongkan outro"
                  aria-label="Kosongkan outro"
                  onClick={() => void kosongkanOutro()}
                >
                  ×
                </button>
              )}
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

            <div className={styles.row}>
              <label>
                <span>Kategori badge (mis. NEWS, HIBURAN, SHOWBIZ)</span>
                <input
                  value={kategori}
                  maxLength={30}
                  placeholder="NEWS"
                  onChange={(e) => setKategori(e.target.value)}
                  onBlur={() => void simpanKategori()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  }}
                />
              </label>
            </div>
            <p className={styles.hint}>
              Ditulis kapital di dalam kotak badge — huruf besarnya menyesuaikan kotak.
              Kosongkan kalau template ini tidak memakai badge.
            </p>

            {/* ---- KOTAK TEKS ---- */}
            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Kotak teks &amp; badge</h3>
                <div className={styles.pilihMode}>
                  <button
                    type="button"
                    className={modeGambar === "teks" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => setModeGambar("teks")}
                  >
                    Kotak teks
                  </button>
                  <button
                    type="button"
                    className={modeGambar === "kategori" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => setModeGambar("kategori")}
                  >
                    Kotak badge
                  </button>
                </div>
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={deteksiKotak}
                    disabled={mendeteksi || !susunan}
                  >
                    {mendeteksi ? "Mencari..." : "Deteksi otomatis"}
                  </button>
                  {modeGambar === "teks" && kotakTeks && (
                    <button type="button" className={styles.hapusBtn} onClick={() => void simpanKotak(null)}>
                      Hapus kotak teks
                    </button>
                  )}
                  {modeGambar === "kategori" && badgeBox && (
                    <button type="button" className={styles.hapusBtn} onClick={() => void simpanBadge(null)}>
                      Pakai tebakan
                    </button>
                  )}
                </div>
              </div>
              <p className={styles.hint}>
                Hijau = kotak teks hook, oranye = kotak badge kategori (putus-putus berarti
                masih tebakan di atas kotak teks). Pilih mode lalu seret di pratinjau untuk
                menggambar ulang. Tiap set layer punya kotaknya sendiri.
              </p>
              {susunan && (
                <div
                  ref={pratinjauRef}
                  className={styles.pratinjauKotak}
                  onPointerDown={mulaiSeret}
                  onPointerMove={gerakSeret}
                  onPointerUp={selesaiSeret}
                  onPointerCancel={selesaiSeret}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`/api/video/templates/${idSet}/preview.png?v=${versiPratinjau}`}
                    alt="Pratinjau layer"
                    draggable={false}
                  />
                  {(() => {
                    const teksK = modeGambar === "teks" && kotakSementara ? kotakSementara : kotakTeks;
                    const badgeK =
                      modeGambar === "kategori" && kotakSementara
                        ? kotakSementara
                        : badgeBox || badgeBoxBawaan;
                    return (
                      <>
                        {teksK && <div className={styles.kotakTeksLayer} style={gayaKotak(teksK)} />}
                        {badgeK && (
                          <div
                            className={`${styles.kotakBadgeLayer} ${!badgeBox && !(modeGambar === "kategori" && kotakSementara) ? styles.kotakBawaan : ""}`}
                            style={gayaKotak(badgeK)}
                          />
                        )}
                      </>
                    );
                  })()}
                </div>
              )}
              {kotakTeks && (
                <p className={styles.hint}>
                  Kotak: x {kotakTeks.x}, y {kotakTeks.y}, lebar {kotakTeks.w}, tinggi {kotakTeks.h}
                </p>
              )}
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

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Template yang dipakai</h3>
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={() => setPilihanSet(daftarSet.map((d) => d.id))}
                  >
                    Pilih semua
                  </button>
                  <button type="button" className={styles.ghostBtn} onClick={() => setPilihanSet([])}>
                    Kosongkan
                  </button>
                </div>
              </div>
              <div className={styles.daftarCek}>
                {daftarSet.map((d) => (
                  <label key={d.id} className={styles.cekItem}>
                    <input
                      type="checkbox"
                      checked={pilihanSet.includes(d.id)}
                      onChange={(e) =>
                        setPilihanSet((p) =>
                          e.target.checked ? [...p, d.id] : p.filter((x) => x !== d.id)
                        )
                      }
                    />
                    <span>{d.name}</span>
                  </label>
                ))}
              </div>
              <p className={styles.hint}>
                Satu link dirender sekali untuk tiap template yang dicentang. Video sumbernya
                diunduh sekali lalu dipakai ulang, dan hasilnya dikerjakan berurutan.
              </p>
            </div>

            <button
              className={styles.mulaiBtn}
              onClick={mulaiRender}
              disabled={mengirim || !pratinjau || pilihanSet.length === 0}
            >
              {mengirim
                ? "Mengirim..."
                : `Buat ${pilihanSet.length} video`}
            </button>

            {jobs.length > 0 && (
              <div className={styles.jobDaftar}>
                {jobs.map((j) => {
                  const berkas = berkasJob(j);
                  return (
                    <div key={j.job_id} className={styles.jobBox}>
                      <div className={styles.jobHead}>
                        <span className={`${styles.badge} ${styles[j.status] || ""}`}>{j.status}</span>
                        <strong>{namaSetDari(j.template_id)}</strong>
                        <span>{j.message}</span>
                      </div>
                      <div className={styles.barWrap}>
                        <div className={styles.bar} style={{ width: `${j.progress || 0}%` }} />
                      </div>
                      {j.status === "error" && j.error && <p className={styles.error}>{j.error}</p>}
                      {j.status === "done" && berkas && (
                        <div className={styles.hasil}>
                          <video className={styles.video} src={berkas} controls playsInline preload="metadata" />
                          <a className={styles.unduhBtn} href={berkas} download>
                            Unduh MP4
                          </a>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
