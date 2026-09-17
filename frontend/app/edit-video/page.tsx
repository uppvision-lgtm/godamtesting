"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";
import { bacaForm, muatFormDariServer, simpanForm } from "../components/formStore";
import LoginButton from "../components/LoginButton";
import { apiFetch, apiUnggah, idTamu, useAkun, useSudahHidrasi } from "../components/auth";
import { useDialog } from "../components/Dialog";

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

// Template pertama. Id "utama" dipertahankan dari versi sebelumnya supaya
// berkas yang sudah diunggah tidak perlu diunggah ulang; namanya kini "TV
// Rakyat". Set berikutnya dibuat lewat tombol "Baru" dengan id sendiri.
const ID_SET_PERTAMA = "utama";
const NAMA_SET_PERTAMA = "TV Rakyat";

const BATAS_UNGGAH_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB || 200);
// Video sumber dari komputer. Lebih kecil dari batas aset karena diproses utuh.
const BATAS_SUMBER_MB = 100;

// Unggahan besar dikirim langsung ke backend, bukan lewat proxy halaman:
// fungsi serverless Vercel membatasi badan permintaan sekitar 4 MB. Kalau
// NEXT_PUBLIC_API_URL tidak diisi (mode lokal), tetap lewat proxy.
const API_LANGSUNG = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/$/, "");
function urlUnggah(path: string): string {
  return API_LANGSUNG ? `${API_LANGSUNG}${path}` : path;
}

/** Id template dari namanya, dengan akhiran acak supaya dua nama mirip tidak bertabrakan. */
function idBaru(nama: string): string {
  return (
    nama.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) +
    "-" +
    Math.random().toString(36).slice(2, 6)
  );
}

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
  // Isi diambil dari field template (mis. "kategori"), bukan dari job.
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
  teks_warna?: string;
  assets?: string[];
  // Diisi server: pemilik template, dan apakah akun yang sedang masuk boleh
  // mengubahnya. Tamu dan template bawaan: false.
  owner?: string;
  can_edit?: boolean;
  aset_dari?: string | null;
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

type BerkasSumber = {
  url: string;
  name: string;
  size: number;
  duration: number | null;
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
  owner?: string;
  created?: number;
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
 *  template, posisinya dari badge_box set itu (atau tebakan di atas kotak
 *  teks kalau belum digambar). */
const TEKS_KATEGORI: TextLayer = {
  name: "kategori",
  style: "kategori",
  source: "kategori",
  color: "white",
  // Ikut jendela waktu kotak bawah: badge menempel pada kotak itu, jadi
  // kalau dibiarkan sampai akhir ia melayang setelah kotaknya hilang.
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
    teks_warna: "white",
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

/**
 * Tanda tanya kecil yang membuka penjelasan saat diklik.
 *
 * Penjelasan panjang disimpan di sini, bukan sebagai paragraf permanen:
 * paragraf abu-abu di bawah tiap kolom jarang dibaca dan justru membuat
 * halaman terasa rumit bagi yang baru pertama memakainya.
 */
function Bantuan({ children }: { children: React.ReactNode }) {
  const [buka, setBuka] = useState(false);
  return (
    <span className={styles.bantuanBungkus}>
      <button
        type="button"
        className={styles.bantuanTombol}
        aria-expanded={buka}
        aria-label={buka ? "Tutup penjelasan" : "Lihat penjelasan"}
        onClick={() => setBuka((b) => !b)}
      >
        ?
      </button>
      {buka && <span className={styles.bantuanIsi}>{children}</span>}
    </span>
  );
}

/** Ubah status job jadi kalimat yang bisa dipahami tanpa tahu istilah teknis. */
function labelStatus(status: string): string {
  switch (status) {
    case "queued":
      return "Menunggu giliran";
    case "downloading":
      return "Mengambil video";
    case "rendering":
      return "Menyusun video";
    case "done":
      return "Selesai";
    case "error":
      return "Gagal";
    case "dibatalkan":
      return "Dihentikan";
    default:
      return status;
  }
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
  return status === "done" || status === "error" || status === "dibatalkan";
}

/** Nama berkas unduhan yang enak dibaca, mis. "TV Rakyat.mp4". */
function namaUnduhan(nama: string, jobId: string): string {
  const bersih = nama.replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim();
  return `${bersih || "video"}-${jobId.slice(0, 6)}.mp4`;
}

export default function EditVideoPage() {
  const awal = bacaForm("edit-video", { link: "" });

  const [link, setLink] = useState(awal.link);
  const [susunan, setSusunan] = useState<Susunan | null>(null);
  const [daftarSet, setDaftarSet] = useState<{ id: string; name: string }[]>([]);
  const [idSet, setIdSet] = useState(ID_SET_PERTAMA);
  const [namaSet, setNamaSet] = useState(NAMA_SET_PERTAMA);
  const [menyimpanNama, setMenyimpanNama] = useState(false);
  // Unggahan yang sedang berjalan. "slot" menandai baris mana yang sedang
  // dipakai: "layer-0".."layer-2", "outro", atau "sumber". Hanya satu
  // unggahan berjalan pada satu waktu, jadi cukup satu keadaan.
  const [unggahan, setUnggahan] = useState<{
    slot: string;
    nama: string;
    persen: number;
    diproses: boolean;
  } | null>(null);
  const [pesanLayer, setPesanLayer] = useState("");
  // Kotak teks set yang sedang dibuka, plus keadaan saat pengguna menggambar.
  const [kotakTeks, setKotakTeks] = useState<KotakTeks | null>(null);
  const [kotakSementara, setKotakSementara] = useState<KotakTeks | null>(null);
  const seretRef = useRef<{ x: number; y: number } | null>(null);
  const pratinjauRef = useRef<HTMLDivElement>(null);
  const [versiPratinjau, setVersiPratinjau] = useState(0);
  // Gambar pratinjau dimuat ulang tiap kali layer berubah; rangka menahan
  // ruangnya supaya isian di bawahnya tidak melompat saat gambar datang.
  const [pratinjauSiap, setPratinjauSiap] = useState(false);
  // Alamat gambar pratinjau. Bukan alamat API langsung, melainkan blob hasil
  // unduhan: tag <img> tidak bisa membawa header Authorization, sedangkan
  // pratinjau template milik akun memang hanya boleh dibuka pemiliknya.
  const [pratinjauSrc, setPratinjauSrc] = useState("");
  const [galatPratinjauGambar, setGalatPratinjauGambar] = useState("");
  const [mendeteksi, setMendeteksi] = useState(false);
  const [kategori, setKategori] = useState("");
  const [teksWarna, setTeksWarna] = useState<"black" | "white">("white");
  const [badgeBox, setBadgeBox] = useState<KotakTeks | null>(null);
  const [badgeBoxBawaan, setBadgeBoxBawaan] = useState<KotakTeks | null>(null);
  // Kotak mana yang sedang digambar di pratinjau.
  const [modeGambar, setModeGambar] = useState<"teks" | "kategori">("teks");
  // Template yang dicentang untuk dirender sekaligus.
  const [pilihanSet, setPilihanSet] = useState<string[]>([]);

  // Sumber video: tempel link, atau unggah berkas dari komputer.
  const [modeSumber, setModeSumber] = useState<"link" | "file">("link");
  const [berkasSumber, setBerkasSumber] = useState<BerkasSumber | null>(null);
  const [mengunggahSumber, setMengunggahSumber] = useState(false);
  const [captionManual, setCaptionManual] = useState("");
  const [sumberManual, setSumberManual] = useState("");
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
  // Angka persen yang DITAMPILKAN, terpisah dari angka yang dilaporkan server.
  // Server hanya ditanya tiap dua detik, jadi angkanya melompat; yang ini
  // berjalan naik satu per satu supaya tidak ada angka yang terlewat.
  const [progresTampil, setProgresTampil] = useState<Record<string, number>>({});
  const [menghentikan, setMenghentikan] = useState(false);
  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");

  // Akun yang sedang masuk. Kosong = tamu: boleh membuat video dengan template
  // bawaan, tetapi tidak bisa menyimpan template maupun riwayat.
  const akun = useAkun();
  const dialog = useDialog();
  // Data akun baru dibaca setelah hidrasi, supaya tamu dan akun tidak
  // sama-sama dimuat lalu saling menimpa.
  const siap = useSudahHidrasi();
  // Template yang dibuka tidak bisa diubah: tamu, atau template bawaan.
  const kunci = !!susunan && !susunan.can_edit;

  useEffect(() => {
    simpanForm("edit-video", { link });
  }, [link]);

  // Link tersimpan di server juga, supaya tidak perlu ditempel ulang setelah
  // tab ditutup. Tamu tidak ikut: isiannya hilang bersama tabnya.
  useEffect(() => {
    let batal = false;
    void muatFormDariServer<{ link?: string }>("edit-video").then((dariServer) => {
      if (!batal && dariServer?.link) setLink(dariServer.link);
    });
    return () => {
      batal = true;
    };
  }, [akun]);

  // Gambar pratinjau diambil dengan apiFetch supaya identitas ikut terkirim,
  // lalu dijadikan alamat blob. Alamat lama dilepas agar tidak menumpuk di
  // memori peramban.
  useEffect(() => {
    let batal = false;
    let alamat = "";
    void (async () => {
      if (!idSet || !susunan) {
        setPratinjauSrc("");
        return;
      }
      try {
        const res = await apiFetch(`/api/video/templates/${idSet}/preview.png?v=${versiPratinjau}`, {
          cache: "no-store",
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          throw new Error(d.detail || "Pratinjau tidak bisa dibuat.");
        }
        const blob = await res.blob();
        if (batal) return;
        alamat = URL.createObjectURL(blob);
        setPratinjauSrc(alamat);
        setGalatPratinjauGambar("");
      } catch (err) {
        if (batal) return;
        setPratinjauSrc("");
        setPratinjauSiap(true);
        setGalatPratinjauGambar(
          err instanceof Error ? err.message : "Pratinjau tidak bisa dibuat."
        );
      }
    })();
    return () => {
      batal = true;
      if (alamat) URL.revokeObjectURL(alamat);
    };
  }, [idSet, susunan, versiPratinjau]);

  // ===== SET LAYER =====

  /** Ambil daftar semua template yang tersimpan. */
  const muatDaftar = useCallback(async () => {
    try {
      const res = await apiFetch("/api/video/templates", { cache: "no-store" });
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

  /** Muat satu template. Kalau belum ada, dibuatkan dengan susunan bawaan. */
  const muatSet = useCallback(async (id: string, nama: string) => {
    try {
      const res = await apiFetch(`/api/video/templates/${id}`, { cache: "no-store" });
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
        setTeksWarna(ada.teks_warna === "black" ? "black" : "white");
        if (perluDisimpan && ada.can_edit) {
          // Penyelarasan ini berjalan sendiri saat set dibuka. Kalau gagal
          // (mis. namanya kembar dengan set lain), setnya tetap ditampilkan
          // supaya masih bisa diperbaiki — hanya penyelarasannya yang tertunda.
          try {
            await simpanSusunan(ada, ada.id);
          } catch (err) {
            setSusunan(ada);
            setPesanLayer(err instanceof Error ? err.message : "Template belum tersimpan.");
          }
        } else {
          setSusunan(ada);
        }
        return;
      }
    } catch {
      // lanjut ke pembuatan di bawah
    }
    try {
      const res = await apiFetch(`/api/video/templates?template_id=${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(susunanBawaan(id, nama)),
      });
      if (res.ok) {
        const dibuat = await res.json();
        setSusunan({ ...dibuat, can_edit: true });
        setIdSet(dibuat.id);
        setNamaSet(dibuat.name);
        setKotakTeks(null);
        setBadgeBox(null);
        setBadgeBoxBawaan(null);
        setKategori("");
        setTeksWarna("white");
        await muatDaftar();
      }
    } catch {
      setPesanLayer("Tidak bisa menyiapkan template.");
    }
    // simpanSusunan sengaja tidak masuk daftar dependensi: ia didefinisikan
    // ulang setiap render, jadi memasukkannya membuat muatSet ikut berubah
    // terus dan effect di bawah berjalan berulang tanpa henti.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [muatDaftar]);

  // Dimuat ulang tiap akun berganti: daftar template tamu dan daftar tiap
  // akun berbeda isinya. Kalau daftarnya kosong, dibuatkan yang pertama
  // dengan id baru (bukan id milik akun lain).
  useEffect(() => {
    if (!siap) return;
    void (async () => {
      const daftar = await muatDaftar();
      const pertama = daftar[0];
      await muatSet(pertama?.id || idBaru(NAMA_SET_PERTAMA), pertama?.name || NAMA_SET_PERTAMA);
    })();
  }, [muatDaftar, muatSet, akun, siap]);

  /** Simpan template (dipanggil setelah berkas diganti atau nama diubah). */
  async function simpanSusunan(baru: Susunan, id?: string) {
    const sasaran = id || idSet;
    setSusunan(baru);
    const res = await apiFetch(`/api/video/templates?template_id=${sasaran}`, {
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
        teks_warna: baru.teks_warna ?? "white",
      }),
    });
    if (!res.ok) {
      // Pesan dari server dipakai apa adanya — mis. saat namanya bentrok.
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Gagal menyimpan template.");
    }
    // Pratinjau digambar dari layer tersimpan, jadi dimuat ulang tiap ada perubahan.
    setVersiPratinjau((v) => v + 1);
    setPratinjauSiap(false);
    // Tebakan kotak badge dihitung server dari kotak teks; ambil yang terbaru.
    try {
      const r = await apiFetch(`/api/video/templates/${sasaran}`, { cache: "no-store" });
      if (r.ok) {
        const d: Susunan = await r.json();
        setBadgeBoxBawaan(d.badge_box_default || null);
      }
    } catch {
      // tebakan lama tetap dipakai
    }
  }

  /** Buang hasil render yang tidak ditampilkan lagi, supaya tidak menumpuk
   *  di penyimpanan. Dipakai saat memulai render baru dan saat halaman
   *  ditinggalkan. */
  async function bersihkanHasil(ids: string[]) {
    if (ids.length === 0) return;
    try {
      await apiFetch("/api/video/jobs/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: ids }),
      });
    } catch {
      // kalau gagal, pembersihan berkala di server yang menanganinya
    }
  }

  /** Salin set yang sedang dibuka jadi set baru dengan nama lain. */
  async function duplikatSet() {
    if (!susunan) return;
    const nama = await dialog.tanya({
      judul: "Duplikat template",
      pesan: `Salinan dari "${namaSet}", lengkap dengan semua berkas layernya.`,
      label: "Nama salinan",
      nilaiAwal: `${namaSet} (salinan)`,
      tombolYa: "Duplikat",
    });
    if (!nama) return;
    setPesanLayer("Menyalin template ...");
    try {
      const res = await apiFetch(`/api/video/templates/${idSet}/duplicate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nama }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal menyalin template.");
      await muatDaftar();
      // Langsung buka salinannya supaya bisa disunting.
      await muatSet(data.id, data.name);
      setPesanLayer(`Template "${nama}" dibuat dari salinan "${namaSet}".`);
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Gagal menyalin template.");
    }
  }

  /** Hapus template yang sedang dibuka beserta berkasnya. */
  async function hapusSet() {
    if (!susunan) return;
    const nama = namaSet;
    const setuju = await dialog.konfirmasi({
      judul: `Hapus template "${nama}"?`,
      pesan: "Semua berkas layer di dalamnya ikut terhapus dan tidak bisa dikembalikan.",
      tombolYa: "Hapus",
      bahaya: true,
    });
    if (!setuju) return;
    try {
      const res = await apiFetch(`/api/video/templates/${idSet}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Gagal menghapus template.");
      const daftar = await muatDaftar();
      const berikut = daftar[0];
      await muatSet(berikut?.id || idBaru(NAMA_SET_PERTAMA), berikut?.name || NAMA_SET_PERTAMA);
      setPesanLayer(`Template "${nama}" dihapus.`);
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Gagal menghapus template.");
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

  async function simpanWarnaTeks(warna: "black" | "white") {
    if (!susunan) return;
    setTeksWarna(warna);
    await simpanSusunan({ ...susunan, teks_warna: warna });
    setPesanLayer(`Warna tulisan diubah jadi ${warna === "black" ? "hitam" : "putih"}.`);
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
      const res = await apiFetch(`/api/video/templates/${idSet}/detect-box`, { method: "POST" });
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
    if (kunci) return;
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
      setPesanLayer("Nama template tidak boleh kosong.");
      return;
    }
    setMenyimpanNama(true);
    setPesanLayer("Menyimpan ...");
    try {
      await simpanSusunan({ ...susunan, name: nama });
      await muatDaftar();
      setPesanLayer(`Template "${nama}" tersimpan.`);
    } catch (err) {
      // Nama yang sudah dipakai set lain ditolak server; kembalikan isian ke
      // nama tersimpan supaya tidak tampak seolah perubahannya berhasil.
      setNamaSet(susunan.name);
      setPesanLayer(err instanceof Error ? err.message : "Gagal menyimpan template.");
    } finally {
      setMenyimpanNama(false);
    }
  }

  /** Buat template baru yang kosong. */
  async function buatSetBaru() {
    const nama = await dialog.tanya({
      judul: "Template baru",
      label: "Nama template",
      nilaiAwal: "",
      petunjuk: akun
        ? "Setelah dibuat, unggah berkas untuk tiap layernya."
        : "Kamu memakai mode tamu, jadi template ini hilang saat tab ditutup.",
      tombolYa: "Buat",
    });
    if (!nama) return;
    // Id dibuat dari namanya supaya mudah dikenali di penyimpanan, dengan
    // akhiran acak agar dua set bernama mirip tidak saling menimpa.
    await muatSet(idBaru(nama), nama);
    setPesanLayer(`Template "${nama}" dibuat. Unggah berkas tiap layernya.`);
  }

  /** Unggah satu berkas, kembalikan namanya ("assets/xxx.png"). */
  async function unggah(file: File, slot: string): Promise<string | null> {
    if (file.size > BATAS_UNGGAH_MB * 1_048_576) {
      setPesanLayer(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar (batas ${BATAS_UNGGAH_MB} MB).`
      );
      return null;
    }
    setPesanLayer("");
    setUnggahan({ slot, nama: file.name, persen: 0, diproses: false });
    try {
      const { ok, data } = await apiUnggah(
        urlUnggah(`/api/video/templates/${idSet}/assets`),
        file,
        (persen) =>
          setUnggahan((u) =>
            u && u.slot === slot ? { ...u, persen, diproses: persen >= 100 } : u
          )
      );
      if (!ok) throw new Error(String(data.detail || "Gagal mengunggah."));
      setPesanLayer(`${file.name} tersimpan.`);
      return data.file as string;
    } catch (err) {
      setPesanLayer(err instanceof Error ? err.message : "Gagal mengunggah.");
      return null;
    } finally {
      setUnggahan((u) => (u && u.slot === slot ? null : u));
    }
  }

  /** Bar kemajuan unggahan untuk satu slot; kosong kalau slot itu tidak sedang dipakai. */
  function barUnggah(slot: string) {
    if (unggahan?.slot !== slot) return null;
    return (
      <div className={styles.unggahProgres}>
        <div className={styles.unggahBarWrap}>
          <div
            className={unggahan.diproses ? styles.unggahBarProses : styles.unggahBar}
            style={unggahan.diproses ? undefined : { width: `${unggahan.persen}%` }}
          />
        </div>
        <span className={styles.unggahLabel}>
          {unggahan.diproses
            ? `Memproses ${unggahan.nama} ...`
            : `Mengunggah ${unggahan.nama} — ${unggahan.persen}%`}
        </span>
      </div>
    );
  }

  async function gantiLayer(index: number, file: File) {
    if (!susunan) return;
    const nama = await unggah(file, `layer-${index}`);
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
    setPesanLayer("Video penutup dikosongkan.");
  }

  async function gantiOutro(file: File) {
    if (!susunan) return;
    const nama = await unggah(file, "outro");
    if (!nama) return;
    await simpanSusunan({ ...susunan, outro: nama });
  }

  // ===== BACA LINK & BUAT HOOK =====

  /** Ubah caption jadi 3 baris lower-third lewat backend. */
  const buatHook = useCallback(async (naskah: string) => {
    if (naskah.trim().length < 3) {
      setKicker("");
      setIsi("");
      setSumberHook("Caption masih kosong, jadi teksnya belum bisa dibuat.");
      return;
    }
    setMembuatHook(true);
    try {
      const res = await apiFetch("/api/video/hook", {
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
        const res = await apiFetch("/api/video/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: tautan }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Tidak bisa membaca link.");
        setPratinjau(data as Pratinjau);
        await buatHook(naskahDariPratinjau(data as Pratinjau));
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

  function naskahDariPratinjau(info: Pratinjau | null): string {
    if (!info) return "";
    return [info.title, info.description].filter(Boolean).join(". ");
  }

  /** Naskah yang jadi bahan teks hook, sesuai mode sumber yang dipilih. */
  function naskahAktif(): string {
    return modeSumber === "file" ? captionManual : naskahDariPratinjau(pratinjau);
  }

  /** Nama pemilik untuk kredit "SUMBER: ..." di bar bawah. */
  const namaSumber = modeSumber === "file" ? sumberManual.trim() : pratinjau?.uploader || "";

  // Ditunda sebentar supaya server tidak dipanggil tiap satu huruf diketik.
  useEffect(() => {
    if (modeSumber !== "link") return;
    const timer = setTimeout(() => void bacaLink(link.trim()), 700);
    return () => clearTimeout(timer);
  }, [link, bacaLink, modeSumber]);

  // Mode berkas: caption diketik sendiri, lalu teks hook disusun darinya.
  useEffect(() => {
    if (modeSumber !== "file") return;
    const timer = setTimeout(() => void buatHook(captionManual.trim()), 900);
    return () => clearTimeout(timer);
  }, [captionManual, buatHook, modeSumber]);

  /** Unggah video sumber dari komputer; hasilnya alamat "upload://..." */
  async function unggahSumber(file: File) {
    if (file.size > BATAS_SUMBER_MB * 1_048_576) {
      setBerkasSumber(null);
      setGalatPratinjau(
        `Video ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar — maksimal ${BATAS_SUMBER_MB} MB.`
      );
      return;
    }
    setMengunggahSumber(true);
    setGalatPratinjau("");
    setUnggahan({ slot: "sumber", nama: file.name, persen: 0, diproses: false });
    try {
      const { ok, data } = await apiUnggah(
        urlUnggah("/api/video/sources"),
        file,
        (persen) =>
          setUnggahan((u) =>
            u && u.slot === "sumber" ? { ...u, persen, diproses: persen >= 100 } : u
          )
      );
      if (!ok) throw new Error(String(data.detail || "Gagal mengunggah video."));
      setBerkasSumber({
        url: data.url as string,
        name: data.name as string,
        size: data.size as number,
        duration: (data.duration as number) ?? null,
      });
    } catch (err) {
      setBerkasSumber(null);
      setGalatPratinjau(err instanceof Error ? err.message : "Gagal mengunggah video.");
    } finally {
      setUnggahan((u) => (u && u.slot === "sumber" ? null : u));
      setMengunggahSumber(false);
    }
  }

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
            const r = await apiFetch(`/api/video/jobs/${id}`, { cache: "no-store" });
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

  // Angka persen dinaikkan satu per satu, satu angka per gambar layar. Server
  // hanya ditanya tiap dua detik sehingga angkanya melompat jauh; tanpa ini
  // yang terlihat cuma 0, lalu 56, lalu 100.
  //
  // Dipakai requestAnimationFrame, bukan pewaktu biasa: dengan pewaktu, dua
  // kenaikan bisa terjadi di antara dua gambar layar sehingga ada angka yang
  // tidak pernah sempat terlihat. Satu kenaikan per gambar menjamin setiap
  // angka benar-benar tampil, dan 60 angka per detik cukup cepat menyusul
  // lompatan sebesar apa pun.
  useEffect(() => {
    let jalan = true;
    let bingkai = 0;
    const langkah = () => {
      if (!jalan) return;
      setProgresTampil((lama) => {
        let berubah = false;
        const baru = { ...lama };
        for (const j of jobsRef.current) {
          const sasaran = j.status === "done" ? 100 : Math.max(0, Math.min(100, j.progress || 0));
          const kini = baru[j.job_id] ?? 0;
          if (kini < sasaran) {
            baru[j.job_id] = kini + 1;
            berubah = true;
          } else if (kini > sasaran) {
            // Job baru memakai ulang slot lama, atau server mundur: samakan.
            baru[j.job_id] = sasaran;
            berubah = true;
          }
        }
        return berubah ? baru : lama;
      });
      bingkai = window.requestAnimationFrame(langkah);
    };
    bingkai = window.requestAnimationFrame(langkah);
    return () => {
      jalan = false;
      window.cancelAnimationFrame(bingkai);
    };
  }, []);

  /** Hentikan semua pembuatan video yang belum selesai. */
  async function hentikanSemua() {
    setMenghentikan(true);
    setGalat("");
    try {
      const res = await apiFetch("/api/video/jobs/stop", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal menghentikan.");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal menghentikan.");
    } finally {
      setMenghentikan(false);
    }
  }

  /** Buang satu hasil dari server, sekaligus dari daftar. */
  async function hapusHasil(jobId: string) {
    setJobs((lama) => lama.filter((j) => j.job_id !== jobId));
    try {
      await apiFetch(`/api/video/jobs/${jobId}`, { method: "DELETE" });
    } catch {
      // kalau gagal, pembersihan berkala di server yang menanganinya
    }
  }

  /** Buang semua hasil yang sudah selesai. */
  async function hapusSemuaHasil() {
    const ids = jobs.filter((j) => selesai(j.status)).map((j) => j.job_id);
    if (ids.length === 0) return;
    const setuju = await dialog.konfirmasi({
      judul: `Hapus ${ids.length} hasil video?`,
      pesan: "Videonya dibuang dari penyimpanan dan tidak bisa dikembalikan.",
      tombolYa: "Hapus",
      bahaya: true,
    });
    if (!setuju) return;
    setJobs((lama) => lama.filter((j) => !ids.includes(j.job_id)));
    void bersihkanHasil(ids);
  }

  // Riwayat akun: job yang masih tersimpan di server muncul lagi setelah
  // halaman dimuat ulang atau setelah masuk. Tamu tidak punya riwayat.
  useEffect(() => {
    if (!siap) return;
    let batal = false;
    void (async () => {
      let riwayat: JobStatus[] = [];
      if (akun) {
        try {
          const res = await apiFetch("/api/video/jobs?limit=50", { cache: "no-store" });
          if (res.ok) riwayat = ((await res.json()).jobs || []) as JobStatus[];
        } catch {
          // riwayat sekadar tidak muncul
        }
      }
      if (!batal) setJobs(riwayat);
    })();
    return () => {
      batal = true;
    };
  }, [akun, siap]);

  // Untuk tamu: saat tab ditutup atau berpindah halaman, hasil yang masih
  // tampil ikut dibuang karena tidak ada riwayat yang menyimpannya. sendBeacon
  // dipakai karena permintaan biasa sering dibatalkan peramban begitu halaman
  // ditinggalkan. Hasil milik akun dibiarkan; server membersihkannya sendiri.
  const jobsRef = useRef<JobStatus[]>([]);
  jobsRef.current = jobs;
  const akunRef = useRef("");
  akunRef.current = akun;
  useEffect(() => {
    const saatKeluar = () => {
      if (akunRef.current) return;
      const ids = jobsRef.current.map((j) => j.job_id);
      if (ids.length === 0) return;
      try {
        navigator.sendBeacon(
          "/api/video/jobs/cleanup",
          // sendBeacon tidak bisa memasang header, jadi id tamu ikut di badan.
          new Blob([JSON.stringify({ job_ids: ids, guest_id: idTamu() })], {
            type: "application/json",
          })
        );
      } catch {
        // tidak apa-apa; pembersihan berkala di server tetap jalan
      }
    };
    window.addEventListener("pagehide", saatKeluar);
    return () => {
      window.removeEventListener("pagehide", saatKeluar);
      saatKeluar();
    };
  }, []);

  function namaSetDari(id?: string): string {
    return daftarSet.find((d) => d.id === id)?.name || id || "";
  }

  /** Alamat untuk TOMBOL UNDUH.
   *
   *  Atribut `download` diabaikan peramban kalau berkasnya beda asal, jadi
   *  menekan tombol dulu justru membuka videonya. Supabase menerima parameter
   *  `download`, dan dengan itu ia mengirim berkasnya sebagai unduhan lengkap
   *  dengan nama yang kita minta. */
  function alamatUnduh(j: JobStatus, berkas: string): string {
    if (!berkas.includes("supabase.co")) return berkas;
    const pemisah = berkas.includes("?") ? "&" : "?";
    return `${berkas}${pemisah}download=${encodeURIComponent(namaUnduhan(namaSetDari(j.template_id), j.job_id))}`;
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
      if (namaSumber) {
        texts.sumber = `SUMBER: ${namaSumber.toUpperCase()}`;
      }
      const sumberUrl = modeSumber === "file" ? berkasSumber?.url || "" : link.trim();
      if (!sumberUrl) {
        throw new Error(
          modeSumber === "file" ? "Unggah dulu video dari komputer." : "Tempel dulu link videonya."
        );
      }
      const sasaran = pilihanSet.length ? pilihanSet : [idSet];
      const res = await apiFetch("/api/video/jobs/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: sumberUrl, template_ids: sasaran, texts }),
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
      if (akun) {
        // Riwayat akun dipertahankan: hasil lama tetap ada di bawah yang baru.
        setJobs((lama) => [...baru, ...lama]);
      } else {
        // Tamu: hasil dari klik sebelumnya tidak ditampilkan lagi, jadi
        // videonya langsung dibuang dari penyimpanan supaya tidak menumpuk.
        void bersihkanHasil(jobs.map((j) => j.job_id));
        setJobs(baru);
      }
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai.");
    } finally {
      setMengirim(false);
    }
  }

  const sedangJalan = jobs.some((j) => !selesai(j.status));

  // Kenapa tombol Buat video belum bisa ditekan. Ditampilkan apa adanya di
  // bawah tombol supaya tidak perlu menebak bagian mana yang kurang.
  const alasanBelumSiap = (() => {
    if (modeSumber === "link" && !pratinjau) {
      return link.trim()
        ? "Tunggu sebentar, isi linknya sedang dibaca."
        : "Tempel dulu link videonya di atas.";
    }
    if (modeSumber === "file" && !berkasSumber) return "Pilih dulu video dari komputer.";
    if (modeSumber === "file" && captionManual.trim().length < 3) return "Isi dulu caption videonya.";
    if (pilihanSet.length === 0) return "Centang dulu minimal satu template di bawah.";
    return "";
  })();

  return (
    <div className={styles.page}>
      {dialog.simpul}
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · EDIT VIDEO</span>
        <span className={styles.headStatus}>{sedangJalan ? "rendering" : "idle"}</span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        {/* ================= LAYER ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Template</h2>
            <span className={styles.headStatus}>720 × 1280</span>
          </div>

          {kunci ? (
            <p className={styles.kunciInfo}>
              Ini template bawaan dan tidak bisa diubah. Tekan Duplikat untuk membuat
              salinan yang bisa kamu sunting{akun ? "" : ", atau + Baru untuk mulai dari kosong"}.
            </p>
          ) : (
            !akun && (
              <p className={styles.tamuInfo}>
                Kamu memakai mode tamu. Semua fitur bisa dipakai, tapi template ini hilang
                saat tab ditutup. Masuk atau daftar supaya tersimpan.
              </p>
            )
          )}

          <div className={styles.form}>
            <div className={styles.row}>
              <label>
                <span>Template</span>
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
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={buatSetBaru}
                title="Buat template kosong"
              >
                + Baru
              </button>
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={duplikatSet}
                disabled={!susunan}
                title="Salin set ini dengan nama baru"
              >
                Duplikat
              </button>
              <button
                type="button"
                className={styles.hapusBtn}
                onClick={hapusSet}
                disabled={!susunan || kunci}
                title="Hapus template yang sedang dibuka"
              >
                Hapus
              </button>
            </div>

            <div className={styles.row}>
              <label>
                <span>Nama template</span>
                <input
                  value={namaSet}
                  maxLength={60}
                  readOnly={kunci}
                  onChange={(e) => setNamaSet(e.target.value)}
                  placeholder="mis. TV Rakyat"
                />
              </label>
              <button
                type="button"
                className={styles.simpanBtn}
                onClick={simpanNama}
                disabled={menyimpanNama || kunci}
              >
                {menyimpanNama ? "Menyimpan..." : "Simpan"}
              </button>
            </div>

            <p className={styles.hint}>
              Ganti berkas tiap bagian di bawah ini.
              <Bantuan>
                Susunan dan waktu tampil tiap bagian sudah diatur, jadi kamu cukup mengganti
                berkasnya. Berkas disimpan terpisah per template, jadi mengubah satu template
                tidak memengaruhi yang lain.
              </Bantuan>
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
                {ov.file && !kunci && (
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
                <label className={kunci ? `${styles.uploadBtn} ${styles.nonaktif}` : styles.uploadBtn}>
                  {ov.file ? "Ganti" : "Unggah"}
                  <input
                    type="file"
                    disabled={kunci || !!unggahan}
                    accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v,.webm"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void gantiLayer(i, f);
                      e.target.value = "";
                    }}
                  />
                </label>
                {barUnggah(`layer-${i}`)}
              </div>
            ))}

            <div className={styles.row}>
              <label>
                <span>Video penutup</span>
                <input
                  readOnly
                  value={susunan?.outro ? susunan.outro.replace("assets/", "") : "(belum ada berkas)"}
                />
              </label>
              {susunan?.outro && !kunci && (
                <button
                  type="button"
                  className={styles.kosongkanBtn}
                  title="Kosongkan video penutup"
                  aria-label="Kosongkan video penutup"
                  onClick={() => void kosongkanOutro()}
                >
                  ×
                </button>
              )}
              <label className={kunci ? `${styles.uploadBtn} ${styles.nonaktif}` : styles.uploadBtn}>
                {susunan?.outro ? "Ganti" : "Unggah"}
                <input
                  type="file"
                  disabled={kunci || !!unggahan}
                  accept=".mp4,.mov,.m4v,.webm"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void gantiOutro(f);
                    e.target.value = "";
                  }}
                />
              </label>
              {barUnggah("outro")}
            </div>

            <div className={styles.row}>
              <label>
                <span>
                  Label kategori
                  <Bantuan>
                    Tulisan di kotak kecil berwarna, mis. NEWS atau HIBURAN. Ditulis kapital,
                    ukurannya menyesuaikan kotak, dan muncul bersamaan dengan panel bawah lalu
                    ikut hilang. Kosongkan kalau template ini tidak memakainya.
                  </Bantuan>
                </span>
                <input
                  value={kategori}
                  maxLength={30}
                  readOnly={kunci}
                  placeholder="NEWS"
                  onChange={(e) => setKategori(e.target.value)}
                  onBlur={() => void simpanKategori()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  }}
                />
              </label>
            </div>




            {/* ---- KOTAK TEKS ---- */}
            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Posisi tulisan</h3>
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
              </div>
              {/* Tombol aksi sengaja ditaruh di luar baris judul. Kalau ikut di
                  dalamnya, jumlah tombol yang berubah-ubah membuat baris kadang
                  membungkus kadang tidak, dan sakelar mode jadi berpindah-pindah
                  antara tengah dan kanan. */}
              <div className={styles.aksi}>
                <button
                  type="button"
                  className={styles.ghostBtn}
                  onClick={deteksiKotak}
                  disabled={mendeteksi || !susunan || kunci}
                >
                  {mendeteksi ? "Mencari..." : "Deteksi otomatis"}
                </button>
                {modeGambar === "teks" && kotakTeks && !kunci && (
                  <button type="button" className={styles.hapusBtn} onClick={() => void simpanKotak(null)}>
                    Hapus kotak teks
                  </button>
                )}
                {modeGambar === "kategori" && badgeBox && !kunci && (
                  <button type="button" className={styles.hapusBtn} onClick={() => void simpanBadge(null)}>
                    Pakai tebakan
                  </button>
                )}
              </div>
              <p className={styles.hint}>
                Seret di gambar untuk menentukan letak tulisan.
                <Bantuan>
                  Hijau adalah kotak tulisan berita, oranye kotak label kategori. Garis
                  putus-putus berarti letaknya masih tebakan otomatis. Pilih dulu kotak mana
                  yang mau diatur lewat tombol di atas, lalu seret di gambar. Tiap template
                  punya letaknya sendiri.
                </Bantuan>
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
                  {!pratinjauSiap && <div className={styles.rangkaKotak} style={{ height: 200 }} />}
                  {galatPratinjauGambar && (
                    <p className={styles.pratinjauGagal}>{galatPratinjauGambar}</p>
                  )}
                  {pratinjauSrc && (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      src={pratinjauSrc}
                      alt="Pratinjau template"
                      draggable={false}
                      style={pratinjauSiap ? undefined : { display: "none" }}
                      onLoad={() => setPratinjauSiap(true)}
                      onError={() => setPratinjauSiap(true)}
                    />
                  )}
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
            <div className={styles.pilihMode} title="Sumber video">
              <button
                type="button"
                className={modeSumber === "link" ? styles.modeAktif : styles.modeBtn}
                onClick={() => setModeSumber("link")}
              >
                Link
              </button>
              <button
                type="button"
                className={modeSumber === "file" ? styles.modeAktif : styles.modeBtn}
                onClick={() => setModeSumber("file")}
              >
                File
              </button>
            </div>
          </div>

          <div className={styles.form}>
            {modeSumber === "link" ? (
              <>
            <label>
              <span>Link video</span>
              <input
                value={link}
                onChange={(e) => setLink(e.target.value)}
                placeholder="https://www.instagram.com/reel/... atau TikTok / YouTube / link MP4"
              />
            </label>

            {(memuatPratinjau || pratinjau || galatPratinjau) && (
              <div className={styles.pratinjauBox}>
                {memuatPratinjau && (
                  <div className={styles.pratinjauIsi} aria-label="Sedang membaca isi link">
                    <div className={`${styles.pratinjauGambar} ${styles.rangka}`} style={{ height: 90 }} />
                    <div className={styles.pratinjauTeks} style={{ flex: 1 }}>
                      <div className={styles.rangkaBaris} style={{ width: "70%" }} />
                      <div className={styles.rangkaBaris} style={{ width: "45%" }} />
                    </div>
                  </div>
                )}
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
              </>
            ) : (
              <>
            <label>
              <span>Video dari komputer (maksimal {BATAS_SUMBER_MB} MB)</span>
              <input
                type="file"
                disabled={mengunggahSumber}
                accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void unggahSumber(f);
                  e.target.value = "";
                }}
              />
            </label>
            {barUnggah("sumber")}
            {galatPratinjau && <p className={styles.error}>{galatPratinjau}</p>}
            {berkasSumber && (
              <p className={styles.hint}>
                Terunggah: <strong>{berkasSumber.name}</strong> (
                {(berkasSumber.size / 1_048_576).toFixed(1)} MB
                {berkasSumber.duration ? `, ${formatDurasi(berkasSumber.duration)}` : ""})
              </p>
            )}
            <label>
              <span>
                  Caption video
                  <Bantuan>
                    Wajib diisi. Tulisan berita di video disusun dari caption ini, jadi
                    ceritakan isi videonya beberapa kalimat.
                  </Bantuan>
                </span>
              <textarea
                className={styles.naskah}
                rows={3}
                value={captionManual}
                onChange={(e) => setCaptionManual(e.target.value)}
                placeholder="Ceritakan isi videonya beberapa kalimat. Tulisan berita disusun dari sini."
              />
            </label>
            <label>
              <span>
                  Pemilik video
                  <Bantuan>
                    Opsional. Ditulis sebagai kredit &quot;SUMBER: ...&quot; di bar bawah video.
                  </Bantuan>
                </span>
              <input
                value={sumberManual}
                maxLength={60}
                onChange={(e) => setSumberManual(e.target.value)}
                placeholder="mis. @namaakun atau TikTok/namaakun"
              />
            </label>
              </>
            )}

            <div className={styles.hookBox}>
              <div className={styles.hookHead}>
                <span className={styles.hookJudul}>Teks di video</span>
                <div className={styles.pilihMode} title="Warna tulisan isi berita">
                  <button
                    type="button"
                    className={teksWarna === "black" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => void simpanWarnaTeks("black")}
                    disabled={kunci}
                  >
                    Hitam
                  </button>
                  <button
                    type="button"
                    className={teksWarna === "white" ? styles.modeAktif : styles.modeBtn}
                    onClick={() => void simpanWarnaTeks("white")}
                    disabled={kunci}
                  >
                    Putih
                  </button>
                </div>
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
                  {membuatHook && (
                    <div aria-label="Sedang menyusun tulisan berita">
                      <div className={styles.rangkaBaris} style={{ width: "92%" }} />
                      <div className={styles.rangkaBaris} style={{ width: "78%" }} />
                    </div>
                  )}
                  {isi && (
                    <p className={`${styles.hookParagraf} ${teksWarna === "white" ? styles.isiPutih : styles.isiHitam}`}>
                      <span className={styles.hookKicker}>{kicker || "VIRAL"}!</span> {isi}
                    </p>
                  )}
                  {!membuatHook && !isi && (
                    <p className={styles.hint}>
                      Tempel link video dulu — teksnya disusun dari caption video itu.
                    </p>
                  )}
                  {sumberHook && <p className={styles.hint}>{sumberHook}</p>}

                  {naskahAktif().trim().length >= 3 && !membuatHook && (
                    <button
                      type="button"
                      className={styles.ghostBtn}
                      onClick={() => void buatHook(naskahAktif())}
                    >
                      Buat ulang
                    </button>
                  )}
                </>
              ) : (
                <>
                  <label>
                    <span>
                      Kata pembuka
                      <Bantuan>
                        Satu kata yang menarik perhatian, mis. VIRAL atau HEBOH. Ditulis merah
                        dan otomatis diberi tanda seru.
                      </Bantuan>
                    </span>
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
                    <p className={`${styles.hookParagraf} ${teksWarna === "white" ? styles.isiPutih : styles.isiHitam}`}>
                      <span className={styles.hookKicker}>
                        {(kickerManual || "VIRAL").replace(/!+$/, "").toUpperCase()}!
                      </span>{" "}
                      {isiManual.toUpperCase()}
                    </p>
                  )}
                  <p className={styles.hint}>
                    Maksimal 4 baris.
                    <Bantuan>
                      Huruf kecil otomatis dijadikan kapital. Kalau tulisannya terlalu panjang
                      untuk empat baris, ukuran hurufnya diperkecil sedikit saat dirender.
                    </Bantuan>
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

              {namaSumber && (
                <p className={styles.kredit}>
                  Kredit di bar bawah: <strong>SUMBER: {namaSumber.toUpperCase()}</strong>
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
                {daftarSet.length === 0 &&
                  [0, 1, 2].map((i) => (
                    <div key={i} className={styles.rangkaKotak} style={{ width: 120, height: 38 }} />
                  ))}
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
                Centang template yang mau dipakai.
                <Bantuan>
                  Satu video dibuat untuk tiap template yang dicentang. Videonya diambil sekali
                  lalu dipakai ulang, dan hasilnya dikerjakan bergantian.
                </Bantuan>
              </p>
            </div>

            <button
              className={styles.mulaiBtn}
              onClick={mulaiRender}
              disabled={mengirim || alasanBelumSiap !== ""}
            >
              {mengirim
                ? "Mengirim..."
                : pilihanSet.length === 1
                  ? "Buat 1 video"
                  : `Buat ${pilihanSet.length} video`}
            </button>
            {alasanBelumSiap && <p className={styles.alasan}>{alasanBelumSiap}</p>}

            {sedangJalan && (
              <button
                type="button"
                className={styles.stopBtn}
                onClick={() => void hentikanSemua()}
                disabled={menghentikan}
              >
                {menghentikan ? "Menghentikan..." : "Hentikan semua"}
              </button>
            )}

            {jobs.length > 0 && (
              <div className={styles.jobDaftar}>
                <div className={styles.jobHeadBar}>
                  <span className={styles.jobJumlah}>
                    {jobs.length} video
                    {jobs.some((j) => selesai(j.status)) &&
                      ` · ${jobs.filter((j) => j.status === "done").length} siap`}
                  </span>
                  {jobs.some((j) => selesai(j.status)) && (
                    <button type="button" className={styles.hapusSemuaBtn} onClick={() => void hapusSemuaHasil()}>
                      Hapus semua hasil
                    </button>
                  )}
                </div>
                {jobs.map((j) => {
                  const berkas = berkasJob(j);
                  return (
                    <div key={j.job_id} className={styles.jobBox}>
                      <div className={styles.jobHead}>
                        <span className={`${styles.badge} ${styles[j.status] || ""}`}>
                          {labelStatus(j.status)}
                        </span>
                        <strong>{namaSetDari(j.template_id)}</strong>
                      </div>
                      {!selesai(j.status) && (
                        <>
                          <div className={styles.barWrap}>
                            {j.status === "rendering" ? (
                              <div
                                className={styles.bar}
                                style={{ width: `${progresTampil[j.job_id] ?? 0}%` }}
                              />
                            ) : (
                              // Tahap unduh tidak punya angka sama sekali, jadi
                              // barnya bergerak alih-alih berdiam di nol.
                              <div className={styles.barGerak} />
                            )}
                          </div>
                          <p className={styles.hint}>
                            {j.status === "rendering"
                              ? `Menyusun video — ${progresTampil[j.job_id] ?? 0}% selesai`
                              : labelStatus(j.status)}
                          </p>
                        </>
                      )}
                      {j.status === "done" && (
                        <p className={styles.hint}>Video siap diputar dan diunduh.</p>
                      )}
                      {j.status === "dibatalkan" && (
                        <p className={styles.hint}>Dihentikan sebelum videonya jadi.</p>
                      )}
                      {j.status === "error" && j.error && <p className={styles.error}>{j.error}</p>}
                      {j.status === "done" && berkas && (
                        <div className={styles.hasil}>
                          <video className={styles.video} src={berkas} controls playsInline preload="metadata" />
                          <div className={styles.hasilAksi}>
                            <a
                              className={styles.unduhBtn}
                              href={alamatUnduh(j, berkas)}
                              download={namaUnduhan(namaSetDari(j.template_id), j.job_id)}
                            >
                              Unduh MP4
                            </a>
                            <button
                              type="button"
                              className={styles.hapusHasilBtn}
                              onClick={() => void hapusHasil(j.job_id)}
                            >
                              Hapus
                            </button>
                          </div>
                        </div>
                      )}
                      {selesai(j.status) && j.status !== "done" && (
                        <button
                          type="button"
                          className={styles.hapusHasilBtn}
                          onClick={() => void hapusHasil(j.job_id)}
                        >
                          Hapus
                        </button>
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
