"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import styles from "./page.module.css";
import LoginButton from "../components/LoginButton";
import LogoSosmed, { WARNA, type PlatformId } from "../components/LogoSosmed";
import { apiFetch, apiUnggah } from "../components/auth";
import { useDialog } from "../components/Dialog";
import { PLATFORM, type Akun } from "../components/sosmed";

/**
 * Auto Upload Sosmed.
 *
 * Satu video atau foto, dikirim ke enam platform sekaligus. Alurnya berurutan
 * dari kiri ke kanan:
 *
 *   tempel Session ID -> pilih berkas -> tulis caption -> centang platform ->
 *   kirim sekarang atau jadwalkan
 *
 * Akun dihubungkan dengan menempel Session ID dari cookie peramban, bukan
 * lewat jendela login. Dengan begitu tidak ada jendela browser yang harus
 * dibuka, dan halaman ini tetap berguna walau dibuka dari perangkat lain.
 *
 * Semua permintaan menuju /api/sosmed, yaitu paket "autoupload" yang dipasang
 * di backend yang sama dengan fitur lain.
 */

type Sasaran = {
  id?: number;
  platform: string;
  account_id?: number | null;
  status: string;
  url: string | null;
  error: string | null;
  /** 0-100. 100 hanya saat targetnya benar-benar selesai. */
  progress?: number;
  /** Nama langkah yang sedang dikerjakan, mis. "melampirkan media". */
  tahap?: string;
};


type Postingan = {
  id: number;
  status: string;
  media_kind: string;
  caption: string;
  scheduled_at: string | null;
  created_at: string | null;
  targets: Sasaran[];
};

type Berkas = { media_path: string; size_bytes: number; media_kind: "video" | "photo" };


const BATAS_BERKAS_MB = 500;

function labelStatus(status: string): string {
  switch (status) {
    case "pending":
      return "Menunggu";
    case "scheduled":
      return "Terjadwal";
    // Backend menulis "processing" dan "done". Tanpa dua baris ini keduanya
    // jatuh ke default dan tampil apa adanya dalam bahasa Inggris.
    case "processing":
    case "running":
      return "Sedang dikirim";
    case "done":
      return "Berhasil";
    case "success":
      return "Berhasil";
    case "partial":
      return "Sebagian berhasil";
    case "failed":
      return "Gagal";
    default:
      return status;
  }
}

function waktuSingkat(nilai: string | null): string {
  if (!nilai) return "";
  const d = new Date(nilai);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("id-ID", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Keterangan kecil di bawah kotak caption: platform mana yang kepanjangan. */
function terpotong(caption: string, dipilih: PlatformId[]): string {
  const lewat = PLATFORM.filter((p) => dipilih.includes(p.id) && caption.length > p.batas);
  if (lewat.length === 0) return "";
  return `Caption dipotong otomatis untuk ${lewat.map((p) => `${p.nama} (${p.batas})`).join(", ")}.`;
}

export default function AutoUploadPage() {
  const dialog = useDialog();

  const [akun, setAkun] = useState<Akun[]>([]);
  const [memuatAkun, setMemuatAkun] = useState(true);
  const [perluLogin, setPerluLogin] = useState(false);
  // Alamat backend untuk unggahan langsung, ditanyakan saat halaman
  // dimuat. Kosong = kirim lewat proxy (mis. saat di localhost).
  const [basisUnggah, setBasisUnggah] = useState("");

  const [berkas, setBerkas] = useState<Berkas | null>(null);
  const [namaBerkas, setNamaBerkas] = useState("");
  const [unggahan, setUnggahan] = useState<{ nama: string; persen: number; diproses: boolean } | null>(
    null
  );

  const [caption, setCaption] = useState("");
  // Tujuan dipilih per AKUN, bukan per platform: satu platform bisa punya
  // banyak akun, masing-masing dengan video dan captionnya sendiri.
  const [akunDipilih, setAkunDipilih] = useState<number[]>([]);
  const [videoAkun, setVideoAkun] = useState<Record<number, Berkas & { nama: string }>>({});
  const [captionAkun, setCaptionAkun] = useState<Record<number, string>>({});
  const [membagiVideo, setMembagiVideo] = useState(false);
  const [jadwal, setJadwal] = useState("");

  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");
  const [hasil, setHasil] = useState<Sasaran[] | null>(null);
  // Angka yang DITAMPILKAN, terpisah dari angka yang dilaporkan server.
  // Server hanya melapor di titik-titik tertentu, dan jeda di antaranya bisa
  // puluhan detik - angka yang membeku selama itu membuat orang mengira
  // prosesnya menggantung.
  const [halus, setHalus] = useState<Record<number, number>>({});

  const [posts, setPosts] = useState<Postingan[]>([]);
  const [memuatPosts, setMemuatPosts] = useState(true);

  // ===== AKUN =====

  const muatAkun = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/accounts", { cache: "no-store" });
      // 401 = belum login. Session ID sosmed setara password, jadi halaman ini
      // memang hanya untuk pemilik akun - tetapi diamnya daftar akun tanpa
      // keterangan membuat orang mengira fiturnya rusak.
      setPerluLogin(res.status === 401);
      if (res.ok) setAkun((await res.json()) as Akun[]);
    } catch {
      // backend sedang mati; daftar dibiarkan seperti adanya
    } finally {
      setMemuatAkun(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await muatAkun();
    })();
  }, [muatAkun]);

  useEffect(() => {
    void (async () => {
      try {
        const res = await apiFetch("/api/sosmed/info", { cache: "no-store" });
        if (res.ok) {
          const d = (await res.json()) as { unggah_langsung?: string };
          setBasisUnggah(d.unggah_langsung || "");
        }
      } catch {
        // tanpa alamatnya, unggahan tetap jalan lewat proxy
      }
    })();
  }, []);

  /** Akun terbaru milik satu platform, atau null kalau belum dihubungkan.
   *
   *  Dipilih berdasarkan id TERBESAR, bukan urutan dalam daftar: urutan yang
   *  dikembalikan server tidak boleh jadi andalan, dan salah pilih di sini
   *  berarti halaman menampilkan akun lama sementara pengunggah memakai yang
   *  baru. */
  /** Akun yang siap dikirimi: sudah ditempel Session ID dan tidak terbukti mati. */
  // Jumlah akun yang terbukti hidup, untuk keterangan di kepala halaman.
  const aktif = akun.filter((a) => a.has_session_id && a.verified === true).length;
  const akunSiap = akun
    .filter((a) => a.has_session_id && a.verified !== false)
    .sort((x, y) => x.platform.localeCompare(y.platform) || x.id - y.id);

  /** Platform yang tersentuh oleh akun-akun terpilih.
   *
   *  Diturunkan, bukan disimpan: peringatan panjang caption dan catatan
   *  jadwal bekerja pada tingkat platform, sedangkan tujuan kini dipilih per
   *  akun. Menyimpan dua daftar terpisah hanya membuat keduanya bisa berbeda.
   */
  const dipilih: PlatformId[] = [
    ...new Set(
      akun.filter((a) => akunDipilih.includes(a.id)).map((a) => a.platform)
    ),
  ];

  // ===== BERKAS =====

  async function pilihBerkas(file: File) {
    if (file.size > BATAS_BERKAS_MB * 1_048_576) {
      setGalat(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar (batas ${BATAS_BERKAS_MB} MB).`
      );
      return;
    }
    setGalat("");
    setBerkas(null);
    setUnggahan({ nama: file.name, persen: 0, diproses: false });
    try {
      // Argumen terakhir: kirim LANGSUNG ke backend kalau NEXT_PUBLIC_BACKEND_URL
      // diset. Proxy Next di Vercel adalah fungsi serverless dengan batas body
      // beberapa megabyte, jadi video besar tidak akan pernah sampai lewat situ.
      const { ok, data } = await apiUnggah(
        "/api/sosmed/media",
        file,
        (persen) => setUnggahan((u) => (u ? { ...u, persen, diproses: persen >= 100 } : u)),
        basisUnggah
      );
      if (!ok) throw new Error(String(data.detail || "Gagal mengunggah berkas."));
      setBerkas(data as unknown as Berkas);
      setNamaBerkas(file.name);
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal mengunggah berkas.");
    } finally {
      setUnggahan(null);
    }
  }

  // ===== RIWAYAT =====

  const muatPosts = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/posts?limit=20", { cache: "no-store" });
      if (res.ok) setPosts((await res.json()) as Postingan[]);
    } catch {
      // riwayat sekadar tidak muncul
    } finally {
      setMemuatPosts(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await muatPosts();
    })();
  }, [muatPosts]);

  // Selama ada yang masih berjalan, riwayat disegarkan sendiri.
  const adaYangJalan = posts.some((p) => p.status === "running" || p.status === "pending");
  useEffect(() => {
    if (!adaYangJalan) return;
    const timer = window.setInterval(() => void muatPosts(), 3000);
    return () => window.clearInterval(timer);
  }, [adaYangJalan, muatPosts]);

  // ===== KIRIM =====

  const alasanBelumSiap = (() => {
    if (!berkas) return "Pilih dulu video atau fotonya.";
    if (!caption.trim()) return "Caption wajib diisi.";
    if (akunDipilih.length === 0) return "Centang dulu minimal satu akun tujuan.";
    if (jadwal) {
      const saat = new Date(jadwal).getTime();
      if (Number.isNaN(saat)) return "Waktu jadwalnya belum lengkap.";
      const menitLagi = (saat - Date.now()) / 60000;
      if (menitLagi <= 0) return "Waktu jadwalnya sudah lewat.";
      // Dicegah di sini, bukan dibiarkan gagal di tengah jalan: videonya
      // sudah terlanjur diunggah beberapa menit sebelum platform menolak.
      const tabrak = PLATFORM.filter(
        (p) =>
          dipilih.includes(p.id) &&
          p.bisaJadwal &&
          ((p.jedaMinimalMenit && menitLagi < p.jedaMinimalMenit) ||
            (p.jedaMaksimalHari && menitLagi > p.jedaMaksimalHari * 1440))
      );
      if (tabrak.length > 0) {
        const p = tabrak[0];
        return menitLagi < (p.jedaMinimalMenit ?? 0)
          ? `${p.nama} menolak jadwal kurang dari ${p.jedaMinimalMenit} menit dari sekarang.`
          : `${p.nama} menolak jadwal lebih dari ${p.jedaMaksimalHari} hari ke depan.`;
      }
    }
    return "";
  })();

  /** Tanyakan status sebuah post sampai selesai.
   *
   *  Upload berjalan menitan di latar belakang server, jadi halaman tidak
   *  lagi menahan satu permintaan panjang - yang dulu membuatnya mustahil
   *  lewat proxy Vercel (putus di 60 detik) dan membuat menutup tab berarti
   *  kehilangan hasilnya.
   */
  async function pantau(postId: number) {
    for (let i = 0; i < 240; i += 1) {
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const res = await apiFetch(`/api/sosmed/posts/${postId}`, { cache: "no-store" });
        if (!res.ok) continue;
        const p = (await res.json()) as Postingan;
        setHasil(p.targets);
        if (["done", "failed", "partial"].includes(p.status)) {
          void muatPosts();
          return;
        }
      } catch {
        // jaringan sesaat; coba lagi pada putaran berikutnya
      }
    }
  }

  /** Unggah beberapa berkas lalu bagikan berurutan ke akun yang tercentang.
   *
   *  Tanpa ini, memberi video berbeda untuk puluhan akun berarti mengunggah
   *  satu per satu - yang tidak masuk akal begitu jumlah akunnya banyak.
   */
  async function bagiVideo(berkasBaru: File[]) {
    setMembagiVideo(true);
    setGalat("");
    try {
      const sasaran = akunDipilih.slice(0, berkasBaru.length);
      if (berkasBaru.length > akunDipilih.length) {
        setGalat(
          `Ada ${berkasBaru.length} berkas tetapi hanya ${akunDipilih.length} akun tercentang; sisanya diabaikan.`
        );
      }
      for (let i = 0; i < sasaran.length; i += 1) {
        const f = berkasBaru[i];
        if (f.size > BATAS_BERKAS_MB * 1_048_576) {
          setGalat(`${f.name} lebih dari ${BATAS_BERKAS_MB} MB; dilewati.`);
          continue;
        }
        setUnggahan({ nama: `${f.name} (${i + 1}/${sasaran.length})`, persen: 0, diproses: false });
        const { ok, data } = await apiUnggah(
          "/api/sosmed/media",
          f,
          (persen) => setUnggahan((u) => (u ? { ...u, persen, diproses: persen >= 100 } : u)),
          basisUnggah
        );
        if (!ok) throw new Error(String(data.detail || `Gagal mengunggah ${f.name}.`));
        const hasilBerkas = data as unknown as Berkas;
        setVideoAkun((lama) => ({
          ...lama,
          [sasaran[i]]: { ...hasilBerkas, nama: f.name },
        }));
      }
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal membagi video.");
    } finally {
      setUnggahan(null);
      setMembagiVideo(false);
    }
  }

  // Merayap pelan ke arah tonggak berikutnya, tetapi tidak pernah melewati
  // laporan server lebih dari 15 angka, dan tidak pernah mencapai 100 selama
  // targetnya belum selesai. Batas itu menjaga dua hal sekaligus: angkanya
  // tidak membeku saat jeda antar-tonggak panjang, dan tidak pernah melesat
  // jauh mendahului kenyataan.
  useEffect(() => {
    if (!hasil) return;
    const jalan = hasil.some((t) => t.status === "pending" || t.status === "running");
    if (!jalan) return;
    const timer = setInterval(() => {
      setHalus((lama) => {
        const baru = { ...lama };
        for (const t of hasil) {
          if (t.id == null) continue;
          const dari = t.progress ?? 0;
          if (t.status === "success") {
            baru[t.id] = 100;
          } else if (t.status === "pending" || t.status === "running") {
            const kini = Math.max(baru[t.id] ?? 0, dari);
            baru[t.id] = Math.min(kini + 0.35, dari + 15, 99);
          }
        }
        return baru;
      });
    }, 900);
    return () => clearInterval(timer);
  }, [hasil]);

  /** Angka yang ditampilkan untuk satu target. */
  function persen(t: Sasaran): number {
    if (t.status === "success") return 100;
    if (t.id == null) return t.progress ?? 0;
    return Math.round(Math.max(halus[t.id] ?? 0, t.progress ?? 0));
  }

  async function kirim() {
    if (!berkas) return;
    setMengirim(true);
    setGalat("");
    setHasil(null);
    setHalus({});
    try {
      const res = await apiFetch("/api/sosmed/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          media_path: berkas.media_path,
          caption: caption.trim(),
          media_kind: berkas.media_kind,
          scheduled_at: jadwal ? new Date(jadwal).toISOString() : null,
          // Satu baris per akun. Yang tidak punya video/caption sendiri
          // memakai yang utama di atas.
          items: akunDipilih
            // Akun yang keburu diputus di panel kiri dilewati; server
            // akan menolak seluruh kiriman kalau ada id yang sudah tidak ada.
            .filter((id) => akunSiap.some((a) => a.id === id))
            .map((id) => ({
            account_id: id,
            media_path: videoAkun[id]?.media_path || "",
            caption: (captionAkun[id] || "").trim(),
          })),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal mengirim.");
      await muatPosts();
      // Server hanya mengembalikan nomor jobnya; hasilnya ditanyakan berkala.
      if (typeof data.post_id === "number") {
        await pantau(data.post_id);
      } else if (data.results) {
        setHasil(data.results as Sasaran[]);
      }
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal mengirim.");
    } finally {
      setMengirim(false);
    }
  }

  /** Hapus satu riwayat beserta berkas medianya di server. */
  async function hapusRiwayat(p: Postingan) {
    const ya = await dialog.konfirmasi({
      judul: "Hapus riwayat ini?",
      pesan: p.caption ? `"${p.caption.slice(0, 60)}"` : "(tanpa caption)",
      tombolYa: "Hapus",
    });
    if (!ya) return;
    try {
      const res = await apiFetch(`/api/sosmed/posts/${p.id}`, { method: "DELETE" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Gagal menghapus.");
      }
      await muatPosts();
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal menghapus.");
    }
  }

  // Yang dihitung adalah yang TERBUKTI bisa masuk, bukan sekadar tersimpan.
  // Angka yang menghitung tempelan akan menyesatkan: akun yang sesinya mati
  // tetap tersimpan, tetapi tidak bisa dipakai mengirim apa pun.

  return (
    <div className={styles.page}>
      {dialog.simpul}

      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">
          ←
        </Link>
        <span className={styles.brand}>AUTOMASI · AUTO UPLOAD</span>
        <span className={styles.headStatus}>
          {aktif > 0 ? `${aktif} akun aktif` : "belum ada akun aktif"}
        </span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        {/* ================= POSTINGAN =================
            Panel akun sengaja TIDAK ada di sini: menempel Session ID sudah
            punya tempatnya sendiri di /kelola-akun, dan menampilkannya di dua
            halaman berarti dua tempat yang bisa berbeda. Halaman ini cukup
            memakai daftar akun yang sudah tersimpan. */}
        <section className={`${styles.panel} ${styles.panelLebar}`}>
          <div className={styles.panelHead}>
            <h2>Buat Postingan</h2>
          </div>

          {perluLogin && (
            <p className={styles.pesan}>
              Masuk dulu untuk memakai Auto Upload. Akun tujuannya diambil dari{" "}
              <Link href="/kelola-akun" className={styles.tautan}>
                Kelola Akun
              </Link>
              .
            </p>
          )}

          <div className={styles.form}>
            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Video atau foto</h3>
              </div>
              <div className={styles.row}>
                <label>
                  <span>Berkas</span>
                  <input readOnly value={namaBerkas || "(belum ada berkas)"} />
                </label>
                <label
                  className={unggahan ? `${styles.uploadBtn} ${styles.nonaktif}` : styles.uploadBtn}
                >
                  {berkas ? "Ganti" : "Pilih"}
                  <input
                    type="file"
                    disabled={!!unggahan}
                    accept="video/*,image/*,.mp4,.mov,.m4v,.webm,.avi,.mkv,.mpg,.mpeg,.3gp,.flv,.wmv,.ts,.m2ts,.jpg,.jpeg,.png,.webp"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void pilihBerkas(f);
                      e.target.value = "";
                    }}
                  />
                </label>
              </div>
              {unggahan && (
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
              )}
              {berkas && !unggahan && (
                <p className={styles.hint}>
                  {berkas.media_kind === "video" ? "Video" : "Foto"} siap,{" "}
                  {(berkas.size_bytes / 1_048_576).toFixed(1)} MB.
                </p>
              )}
            </div>

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Caption</h3>
                <span className={styles.jumlahHuruf}>{caption.length} huruf</span>
              </div>
              <textarea
                className={styles.naskah}
                rows={4}
                value={caption}
                onChange={(e) => setCaption(e.target.value)}
                placeholder="Tulis sekali, dipakai untuk semua platform."
              />
              {terpotong(caption, dipilih) && (
                <p className={styles.peringatan}>{terpotong(caption, dipilih)}</p>
              )}
            </div>

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Akun tujuan</h3>
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    onClick={() => setAkunDipilih(akunSiap.map((a) => a.id))}
                  >
                    Pilih semua
                  </button>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    onClick={() => setAkunDipilih([])}
                  >
                    Kosongkan
                  </button>
                  <label className={styles.btnSekunder} title="Pilih beberapa video sekaligus; video ke-1 untuk akun tercentang ke-1, dan seterusnya">
                    {membagiVideo ? "Membagi..." : "Beda video per akun"}
                    <input
                      type="file"
                      accept="video/*,image/*,.mp4,.mov,.m4v,.webm,.avi,.mkv,.mpg,.mpeg,.3gp,.flv,.wmv,.ts,.m2ts,.jpg,.jpeg,.png,.webp"
                      multiple
                      hidden
                      disabled={membagiVideo || akunDipilih.length === 0}
                      onChange={(e) => {
                        const f = Array.from(e.target.files || []);
                        e.target.value = "";
                        if (f.length) void bagiVideo(f);
                      }}
                    />
                  </label>
                  <Link href="/kelola-akun" className={styles.btnSekunder}>
                    Kelola akun
                  </Link>
                </div>
              </div>
              {akunSiap.length === 0 ? (
                <p className={styles.hint}>
                  Belum ada akun yang siap. Tempel Session ID-nya di panel kiri.
                </p>
              ) : (
                <div className={styles.daftarTujuan}>
                  {akunSiap.map((a) => {
                    const pf = PLATFORM.find((x) => x.id === a.platform);
                    const dicentang = akunDipilih.includes(a.id);
                    const khusus = videoAkun[a.id];
                    return (
                      <div
                        key={a.id}
                        className={`${styles.tujuanBaris} ${dicentang ? styles.tujuanAktif : ""}`}
                      >
                        <label className={styles.tujuanPilih}>
                          <input
                            type="checkbox"
                            checked={dicentang}
                            onChange={(e) =>
                              setAkunDipilih((lama) =>
                                e.target.checked
                                  ? [...lama, a.id]
                                  : lama.filter((x) => x !== a.id)
                              )
                            }
                          />
                          <span className={styles.logoKecil} style={{ color: WARNA[a.platform] }}>
                            <LogoSosmed platform={a.platform} ukuran={15} />
                          </span>
                          <span className={styles.tujuanNama}>
                            {a.nama_akun || a.session_name}
                            {pf?.belumTeruji && (
                              <span className={styles.tandaUji}>belum teruji</span>
                            )}
                          </span>
                        </label>

                        <input
                          className={styles.tujuanCaption}
                          value={captionAkun[a.id] || ""}
                          placeholder="caption khusus (kosong = pakai caption di atas)"
                          spellCheck={false}
                          onChange={(e) =>
                            setCaptionAkun((lama) => ({ ...lama, [a.id]: e.target.value }))
                          }
                        />

                        <span className={styles.tujuanVideo} title={khusus ? khusus.nama : ""}>
                          {khusus ? khusus.nama : "video utama"}
                        </span>
                        {khusus && (
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            title="Kembalikan ke video utama"
                            onClick={() =>
                              setVideoAkun((lama) => {
                                const s = { ...lama };
                                delete s[a.id];
                                return s;
                              })
                            }
                          >
                            Reset
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Jadwal</h3>
              </div>
              <div className={styles.row}>
                <label>
                  <span>Kosongkan untuk mengirim sekarang</span>
                  <input
                    type="datetime-local"
                    value={jadwal}
                    onChange={(e) => setJadwal(e.target.value)}
                  />
                </label>
                {jadwal && (
                  <button type="button" className={styles.btnSekunder} onClick={() => setJadwal("")}>
                    Hapus jadwal
                  </button>
                )}
              </div>
              {jadwal && (
                <div className={styles.catatanJadwal}>
                  <p className={styles.hint}>
                    Jadwalnya dititipkan ke fitur penjadwalan milik sosmed itu sendiri, jadi
                    videonya diunggah sekarang lalu platformnya yang menerbitkan pada waktunya.
                    Laptop boleh dimatikan setelah proses ini selesai.
                  </p>
                  {PLATFORM.filter(
                    (p) => dipilih.includes(p.id) && p.bisaJadwal && p.catatanJadwal
                  ).map((p) => (
                    <p key={p.id} className={styles.hint}>
                      <b>{p.nama}:</b> {p.catatanJadwal}.
                    </p>
                  ))}
                  {(() => {
                    const tak = PLATFORM.filter((p) => dipilih.includes(p.id) && !p.bisaJadwal);
                    if (tak.length === 0) return null;
                    return (
                      <p className={styles.peringatan}>
                        Tidak bisa dijadwalkan dan akan <b>gagal</b> selama waktu jadwal terisi:{" "}
                        {tak.map((p) => `${p.nama} (${p.alasanJadwal})`).join("; ")}. Hapus
                        jadwalnya untuk mengirim sekarang, atau keluarkan platform itu dari tujuan.
                      </p>
                    );
                  })()}
                </div>
              )}
            </div>

            {galat && (
              <p className={styles.galat} role="alert">
                {galat}
              </p>
            )}

            <button
              type="button"
              className={styles.btnUtama}
              disabled={!!alasanBelumSiap || mengirim || !!unggahan}
              onClick={() => void kirim()}
            >
              {mengirim
                ? "Mengirim..."
                : jadwal
                  ? "Jadwalkan"
                  : `Kirim ke ${akunDipilih.length || 0} akun`}
            </button>
            {alasanBelumSiap && <p className={styles.alasan}>{alasanBelumSiap}</p>}

            {hasil && hasil.length > 0 && (
              <div className={styles.ringkasProgres}>
                <span className={styles.progresAngka}>
                  {Math.round(hasil.reduce((j, t) => j + persen(t), 0) / hasil.length)}%
                </span>
                <span className={styles.progresBatang}>
                  <span
                    className={styles.progresIsi}
                    style={{
                      width: `${hasil.reduce((j, t) => j + persen(t), 0) / hasil.length}%`,
                    }}
                  />
                </span>
                <span className={styles.progresTahap}>
                  {hasil.filter((t) => t.status === "success" || t.status === "failed").length}
                  {" dari "}
                  {hasil.length} akun selesai
                </span>
              </div>
            )}

            {hasil && (
              <div className={styles.hasilKirim}>
                {hasil.map((r) => {
                  const p = PLATFORM.find((x) => x.id === r.platform);
                  return (
                    <div key={r.platform} className={styles.hasilBaris}>
                      <span className={styles.hasilNama}>
                        {p && (
                          <span className={styles.logoKecil} style={{ color: WARNA[p.id] }}>
                            <LogoSosmed platform={p.id} ukuran={15} />
                          </span>
                        )}
                        <span className={r.status === "success" ? styles.sambung : styles.putus}>
                          {p?.nama || r.platform}
                        </span>
                      </span>
                      {r.status === "pending" || r.status === "running" ? (
                        <span className={styles.hasilProgres}>
                          <span className={styles.progresAngka}>{persen(r)}%</span>
                          <span className={styles.progresBatang}>
                            <span
                              className={styles.progresIsi}
                              style={{ width: `${persen(r)}%` }}
                            />
                          </span>
                          <span className={styles.progresTahap}>
                            {r.tahap || "menyiapkan"}
                          </span>
                        </span>
                      ) : r.url ? (
                        <span className={styles.hasilTautan}>
                          <a href={r.url} target="_blank" rel="noreferrer" className={styles.tautan}>
                            Lihat postingan
                          </a>
                          <span className={styles.alamatPost}>{r.url}</span>
                        </span>
                      ) : (
                        <span className={styles.hint}>{r.error || labelStatus(r.status)}</span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        {/* ================= RIWAYAT ================= */}
        <section className={`${styles.panel} ${styles.panelLebar}`}>
          <div className={styles.panelHead}>
            <h2>Riwayat &amp; Jadwal</h2>
            <button type="button" className={styles.btnSekunder} onClick={() => void muatPosts()}>
              Muat ulang
            </button>
          </div>

          {memuatPosts && <div className={styles.rangka} />}
          {!memuatPosts && posts.length === 0 && (
            <p className={styles.hint}>Belum ada postingan. Yang kamu kirim akan muncul di sini.</p>
          )}

          <div className={styles.daftarPost}>
            {posts.map((p) => (
              <div key={p.id} className={styles.postBox}>
                <div className={styles.postHead}>
                  <span className={`${styles.badge} ${styles[p.status] || ""}`}>
                    {labelStatus(p.status)}
                  </span>
                  <span className={styles.hint}>
                    {p.scheduled_at
                      ? `Dijadwalkan ${waktuSingkat(p.scheduled_at)}`
                      : waktuSingkat(p.created_at)}
                  </span>
                  <button
                    type="button"
                    className={styles.hapusRiwayat}
                    title="Hapus riwayat ini beserta berkas medianya"
                    disabled={p.status === "processing"}
                    onClick={() => void hapusRiwayat(p)}
                  >
                    Hapus
                  </button>
                </div>
                <p className={styles.postCaption}>{p.caption || "(tanpa caption)"}</p>
                <div className={styles.postSasaran}>
                  {p.targets.map((t) => {
                    const pf = PLATFORM.find((x) => x.id === t.platform);
                    return (
                      <span
                        key={t.platform}
                        className={`${styles.sasaran} ${t.status === "success" ? styles.sasaranOk : t.status === "failed" ? styles.sasaranGagal : ""}`}
                        title={t.error || ""}
                      >
                        {pf && (
                          <span className={styles.logoKecil} style={{ color: WARNA[pf.id] }}>
                            <LogoSosmed platform={pf.id} ukuran={13} />
                          </span>
                        )}
                        {pf?.nama || t.platform}
                        {t.url && (
                          <a
                            href={t.url}
                            target="_blank"
                            rel="noreferrer"
                            className={styles.tautanKecil}
                          >
                            buka
                          </a>
                        )}
                      </span>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
