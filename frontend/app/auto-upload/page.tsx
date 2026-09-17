"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import styles from "./page.module.css";
import LoginButton from "../components/LoginButton";
import { apiFetch, apiUnggah } from "../components/auth";
import { useDialog } from "../components/Dialog";

/**
 * Auto Upload Sosmed.
 *
 * Satu video atau foto, dikirim ke enam platform sekaligus lewat sesi browser
 * sungguhan. Alurnya sengaja dibuat berurutan dari kiri ke kanan:
 *
 *   hubungkan akun -> pilih berkas -> tulis caption -> centang platform ->
 *   unggah sekarang atau jadwalkan
 *
 * Semua permintaan menuju /api/sosmed, yaitu paket "autoupload" yang dipasang
 * di backend yang sama dengan fitur lain.
 */

type Platform = "tiktok" | "instagram" | "facebook" | "youtube" | "threads" | "twitter";

type Sesi = {
  platform: Platform;
  running: boolean;
  message: string;
  has_session: boolean;
  saved_at: string | null;
  session_name: string;
  sessions: string[];
};

type Sasaran = { platform: string; status: string; url: string | null; error: string | null };

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

/** Nama tampil dan warna tiap platform. Urutannya mengikuti kebiasaan pakai. */
const PLATFORM: { id: Platform; nama: string; warna: string; batas: number }[] = [
  { id: "instagram", nama: "Instagram", warna: "#e1306c", batas: 2200 },
  { id: "tiktok", nama: "TikTok", warna: "#25f4ee", batas: 2200 },
  { id: "youtube", nama: "YouTube", warna: "#ff0000", batas: 5000 },
  { id: "facebook", nama: "Facebook", warna: "#1877f2", batas: 5000 },
  { id: "threads", nama: "Threads", warna: "#c9c9c9", batas: 500 },
  { id: "twitter", nama: "X / Twitter", warna: "#9aa89f", batas: 280 },
];

const BATAS_BERKAS_MB = 500;

function labelStatus(status: string): string {
  switch (status) {
    case "pending":
      return "Menunggu";
    case "scheduled":
      return "Terjadwal";
    case "running":
      return "Sedang dikirim";
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
function terpotong(caption: string, dipilih: Platform[]): string {
  const lewat = PLATFORM.filter((p) => dipilih.includes(p.id) && caption.length > p.batas);
  if (lewat.length === 0) return "";
  return `Caption dipotong otomatis untuk ${lewat.map((p) => `${p.nama} (${p.batas})`).join(", ")}.`;
}

export default function AutoUploadPage() {
  const dialog = useDialog();

  const [sesi, setSesi] = useState<Sesi[]>([]);
  const [memuatSesi, setMemuatSesi] = useState(true);
  const [sibukPlatform, setSibukPlatform] = useState<string>("");

  const [berkas, setBerkas] = useState<Berkas | null>(null);
  const [namaBerkas, setNamaBerkas] = useState("");
  const [unggahan, setUnggahan] = useState<{ nama: string; persen: number; diproses: boolean } | null>(
    null
  );

  const [caption, setCaption] = useState("");
  const [dipilih, setDipilih] = useState<Platform[]>([]);
  const [jadwal, setJadwal] = useState("");

  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");
  const [hasil, setHasil] = useState<Sasaran[] | null>(null);

  const [posts, setPosts] = useState<Postingan[]>([]);
  const [memuatPosts, setMemuatPosts] = useState(true);

  // ===== AKUN & SESI =====

  const muatSesi = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/login", { cache: "no-store" });
      if (res.ok) setSesi((await res.json()) as Sesi[]);
    } catch {
      // backend sedang mati; daftar dibiarkan seperti adanya
    } finally {
      setMemuatSesi(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await muatSesi();
    })();
  }, [muatSesi]);

  // Selama ada jendela login terbuka, statusnya ditanyakan berkala supaya
  // tombol "Selesai" hilang sendiri begitu sesinya tersimpan.
  const adaYangLogin = sesi.some((s) => s.running);
  useEffect(() => {
    if (!adaYangLogin) return;
    const timer = window.setInterval(() => void muatSesi(), 2000);
    return () => window.clearInterval(timer);
  }, [adaYangLogin, muatSesi]);

  async function aksiLogin(platform: string, aksi: "start" | "finish" | "cancel") {
    setSibukPlatform(platform);
    setGalat("");
    try {
      const res = await apiFetch(`/api/sosmed/login/${platform}/${aksi}`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal.");
      await muatSesi();
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal.");
    } finally {
      setSibukPlatform("");
    }
  }

  async function hapusSesi(platform: string, nama: string) {
    const setuju = await dialog.konfirmasi({
      judul: `Putuskan sesi ${nama}?`,
      pesan: "Kamu perlu login lewat jendela browser lagi untuk memakai platform ini.",
      tombolYa: "Putuskan",
      bahaya: true,
    });
    if (!setuju) return;
    setSibukPlatform(platform);
    try {
      await apiFetch(`/api/sosmed/login/${platform}/${encodeURIComponent(nama)}`, {
        method: "DELETE",
      });
      await muatSesi();
    } finally {
      setSibukPlatform("");
    }
  }

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
      const { ok, data } = await apiUnggah("/api/sosmed/media", file, (persen) =>
        setUnggahan((u) => (u ? { ...u, persen, diproses: persen >= 100 } : u))
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

  // Selama ada yang masih berjalan atau terjadwal, riwayat disegarkan sendiri.
  const adaYangJalan = posts.some((p) => p.status === "running" || p.status === "pending");
  useEffect(() => {
    if (!adaYangJalan) return;
    const timer = window.setInterval(() => void muatPosts(), 3000);
    return () => window.clearInterval(timer);
  }, [adaYangJalan, muatPosts]);

  // ===== KIRIM =====

  const punyaSesi = (p: Platform) => sesi.find((s) => s.platform === p)?.has_session ?? false;

  const alasanBelumSiap = (() => {
    if (!berkas) return "Pilih dulu video atau fotonya.";
    if (!caption.trim()) return "Caption wajib diisi.";
    if (dipilih.length === 0) return "Centang dulu minimal satu platform tujuan.";
    const tanpaSesi = dipilih.filter((p) => !punyaSesi(p));
    if (tanpaSesi.length > 0) {
      const nama = tanpaSesi.map((p) => PLATFORM.find((x) => x.id === p)?.nama).join(", ");
      return `Hubungkan dulu akun ${nama} di panel kiri.`;
    }
    return "";
  })();

  async function kirim() {
    if (!berkas) return;
    setMengirim(true);
    setGalat("");
    setHasil(null);
    try {
      const res = await apiFetch("/api/sosmed/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          media_path: berkas.media_path,
          caption: caption.trim(),
          platforms: dipilih,
          media_kind: berkas.media_kind,
          scheduled_at: jadwal ? new Date(jadwal).toISOString() : null,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal mengirim.");
      if (data.results) setHasil(data.results as Sasaran[]);
      await muatPosts();
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal mengirim.");
    } finally {
      setMengirim(false);
    }
  }

  const terhubung = sesi.filter((s) => s.has_session).length;

  return (
    <div className={styles.page}>
      {dialog.simpul}

      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">
          ←
        </Link>
        <span className={styles.brand}>IG · AUTO UPLOAD</span>
        <span className={styles.headStatus}>
          {terhubung > 0 ? `${terhubung} akun terhubung` : "belum ada akun"}
        </span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        {/* ================= AKUN ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Akun Sosmed</h2>
            <span className={styles.headStatus}>{terhubung} / {PLATFORM.length}</span>
          </div>

          <p className={styles.hint}>
            Login sekali lewat jendela browser sungguhan. Sesinya disimpan di laptop ini, jadi
            tidak perlu mengulang tiap mengunggah.
          </p>

          <div className={styles.daftarAkun}>
            {memuatSesi &&
              [0, 1, 2].map((i) => <div key={i} className={styles.rangka} />)}

            {!memuatSesi &&
              PLATFORM.map((p) => {
                const s = sesi.find((x) => x.platform === p.id);
                const sibuk = sibukPlatform === p.id;
                return (
                  <div key={p.id} className={styles.akunBaris}>
                    <span className={styles.titik} style={{ background: p.warna }} aria-hidden="true" />
                    <div className={styles.akunTeks}>
                      <strong>{p.nama}</strong>
                      <span className={s?.has_session ? styles.sambung : styles.putus}>
                        {s?.running
                          ? "Jendela login terbuka"
                          : s?.has_session
                            ? `Terhubung · ${s.sessions.join(", ")}`
                            : "Belum terhubung"}
                      </span>
                    </div>
                    <div className={styles.akunAksi}>
                      {s?.running ? (
                        <>
                          <button
                            type="button"
                            className={styles.btnUtama}
                            disabled={sibuk}
                            onClick={() => void aksiLogin(p.id, "finish")}
                          >
                            Selesai
                          </button>
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            disabled={sibuk}
                            onClick={() => void aksiLogin(p.id, "cancel")}
                          >
                            Batal
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            disabled={sibuk}
                            onClick={() => void aksiLogin(p.id, "start")}
                          >
                            {sibuk ? "..." : s?.has_session ? "Login ulang" : "Hubungkan"}
                          </button>
                          {s?.has_session &&
                            s.sessions.map((n) => (
                              <button
                                key={n}
                                type="button"
                                className={styles.btnBahaya}
                                disabled={sibuk}
                                title={`Putuskan sesi ${n}`}
                                onClick={() => void hapusSesi(p.id, n)}
                              >
                                Putuskan
                              </button>
                            ))}
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>

          {sesi.find((s) => s.running)?.message && (
            <p className={styles.pesan}>{sesi.find((s) => s.running)?.message}</p>
          )}
        </section>

        {/* ================= POSTINGAN ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Buat Postingan</h2>
          </div>

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
                <label className={unggahan ? `${styles.uploadBtn} ${styles.nonaktif}` : styles.uploadBtn}>
                  {berkas ? "Ganti" : "Pilih"}
                  <input
                    type="file"
                    disabled={!!unggahan}
                    accept="video/*,image/*"
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
                <h3>Platform tujuan</h3>
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    onClick={() => setDipilih(PLATFORM.filter((p) => punyaSesi(p.id)).map((p) => p.id))}
                  >
                    Pilih semua
                  </button>
                  <button type="button" className={styles.btnSekunder} onClick={() => setDipilih([])}>
                    Kosongkan
                  </button>
                </div>
              </div>
              <div className={styles.daftarCek}>
                {PLATFORM.map((p) => {
                  const bisa = punyaSesi(p.id);
                  const aktif = dipilih.includes(p.id);
                  return (
                    <label
                      key={p.id}
                      className={`${styles.cekItem} ${aktif ? styles.cekAktif : ""} ${bisa ? "" : styles.cekMati}`}
                      title={bisa ? "" : "Hubungkan akunnya dulu di panel kiri"}
                    >
                      <input
                        type="checkbox"
                        checked={aktif}
                        disabled={!bisa}
                        onChange={(e) =>
                          setDipilih((lama) =>
                            e.target.checked ? [...lama, p.id] : lama.filter((x) => x !== p.id)
                          )
                        }
                      />
                      <span className={styles.titik} style={{ background: p.warna }} aria-hidden="true" />
                      {p.nama}
                    </label>
                  );
                })}
              </div>
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
                  : `Kirim ke ${dipilih.length || 0} platform`}
            </button>
            {alasanBelumSiap && <p className={styles.alasan}>{alasanBelumSiap}</p>}

            {hasil && (
              <div className={styles.hasilKirim}>
                {hasil.map((r) => (
                  <div key={r.platform} className={styles.hasilBaris}>
                    <span className={r.status === "success" ? styles.sambung : styles.putus}>
                      {PLATFORM.find((p) => p.id === r.platform)?.nama || r.platform}
                    </span>
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" className={styles.tautan}>
                        Lihat postingan
                      </a>
                    ) : (
                      <span className={styles.hint}>{r.error || labelStatus(r.status)}</span>
                    )}
                  </div>
                ))}
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
                </div>
                <p className={styles.postCaption}>{p.caption || "(tanpa caption)"}</p>
                <div className={styles.postSasaran}>
                  {p.targets.map((t) => (
                    <span
                      key={t.platform}
                      className={`${styles.sasaran} ${t.status === "success" ? styles.sasaranOk : t.status === "failed" ? styles.sasaranGagal : ""}`}
                      title={t.error || ""}
                    >
                      {PLATFORM.find((x) => x.id === t.platform)?.nama || t.platform}
                      {t.url && (
                        <a href={t.url} target="_blank" rel="noreferrer" className={styles.tautanKecil}>
                          buka
                        </a>
                      )}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
