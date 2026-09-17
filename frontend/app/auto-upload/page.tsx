"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import styles from "./page.module.css";
import LoginButton from "../components/LoginButton";
import LogoSosmed, { WARNA, type PlatformId } from "../components/LogoSosmed";
import { apiFetch, apiUnggah } from "../components/auth";
import { useDialog } from "../components/Dialog";

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

type Sasaran = { platform: string; status: string; url: string | null; error: string | null };

type Akun = {
  id: number;
  platform: PlatformId;
  display_name: string;
  session_name: string;
  has_session_id: boolean;
  has_browser_session: boolean;
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

/** Nama tampil, batas caption, dan cookie yang harus ditempel tiap platform.
 *
 *  Daftar cookie-nya mengikuti session_cookie_map di paket autoupload. Yang
 *  ditulis di sini hanya yang wajib; milik YouTube ada tiga cookie tambahan
 *  yang boleh dilewati. */
const PLATFORM: {
  id: PlatformId;
  nama: string;
  batas: number;
  cookie: string[];
  petunjuk: string;
}[] = [
  {
    id: "instagram",
    nama: "Instagram",
    batas: 2200,
    cookie: ["sessionid"],
    petunjuk: "Buka instagram.com, lalu salin nilai cookie sessionid.",
  },
  {
    id: "tiktok",
    nama: "TikTok",
    batas: 2200,
    cookie: ["sessionid"],
    petunjuk: "Buka tiktok.com, lalu salin nilai cookie sessionid.",
  },
  {
    id: "youtube",
    nama: "YouTube",
    batas: 5000,
    cookie: ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"],
    petunjuk: "Buka youtube.com, salin ketujuh cookie di atas, pisahkan dengan tanda |.",
  },
  {
    id: "facebook",
    nama: "Facebook",
    batas: 5000,
    cookie: ["c_user", "xs"],
    petunjuk: "Buka facebook.com, salin c_user dan xs, pisahkan dengan tanda |.",
  },
  {
    id: "threads",
    nama: "Threads",
    batas: 500,
    cookie: ["sessionid"],
    petunjuk: "Buka threads.net, lalu salin nilai cookie sessionid.",
  },
  {
    id: "twitter",
    nama: "X / Twitter",
    batas: 280,
    cookie: ["auth_token"],
    petunjuk: "Buka x.com, lalu salin nilai cookie auth_token.",
  },
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
function terpotong(caption: string, dipilih: PlatformId[]): string {
  const lewat = PLATFORM.filter((p) => dipilih.includes(p.id) && caption.length > p.batas);
  if (lewat.length === 0) return "";
  return `Caption dipotong otomatis untuk ${lewat.map((p) => `${p.nama} (${p.batas})`).join(", ")}.`;
}

export default function AutoUploadPage() {
  const dialog = useDialog();

  const [akun, setAkun] = useState<Akun[]>([]);
  const [memuatAkun, setMemuatAkun] = useState(true);
  const [sibuk, setSibuk] = useState<string>("");
  // Platform yang kotak Session ID-nya sedang terbuka, beserta isiannya.
  const [buka, setBuka] = useState<PlatformId | "">("");
  const [ssid, setSsid] = useState("");
  const [galatAkun, setGalatAkun] = useState("");

  const [berkas, setBerkas] = useState<Berkas | null>(null);
  const [namaBerkas, setNamaBerkas] = useState("");
  const [unggahan, setUnggahan] = useState<{ nama: string; persen: number; diproses: boolean } | null>(
    null
  );

  const [caption, setCaption] = useState("");
  const [dipilih, setDipilih] = useState<PlatformId[]>([]);
  const [jadwal, setJadwal] = useState("");

  const [mengirim, setMengirim] = useState(false);
  const [galat, setGalat] = useState("");
  const [hasil, setHasil] = useState<Sasaran[] | null>(null);

  const [posts, setPosts] = useState<Postingan[]>([]);
  const [memuatPosts, setMemuatPosts] = useState(true);

  // ===== AKUN =====

  const muatAkun = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/accounts", { cache: "no-store" });
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

  /** Akun terbaru milik satu platform, atau null kalau belum dihubungkan. */
  const akunDari = (p: PlatformId) =>
    akun.filter((a) => a.platform === p && a.has_session_id).slice(-1)[0] || null;

  async function simpanSsid(p: (typeof PLATFORM)[number]) {
    const nilai = ssid.trim();
    if (!nilai) return;
    const bagian = nilai.split("|").length;
    if (bagian !== p.cookie.length) {
      setGalatAkun(
        `${p.nama} butuh ${p.cookie.length} nilai (${p.cookie.join(" | ")}), yang ditempel ${bagian}.`
      );
      return;
    }
    setSibuk(p.id);
    setGalatAkun("");
    try {
      const res = await apiFetch("/api/sosmed/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: p.id,
          display_name: p.nama,
          session_name: "ssid",
          session_id: nilai,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal menyimpan Session ID.");
      setSsid("");
      setBuka("");
      await muatAkun();
    } catch (err) {
      setGalatAkun(err instanceof Error ? err.message : "Gagal menyimpan Session ID.");
    } finally {
      setSibuk("");
    }
  }

  async function putuskan(p: (typeof PLATFORM)[number], a: Akun) {
    const setuju = await dialog.konfirmasi({
      judul: `Putuskan ${p.nama}?`,
      pesan: "Session ID-nya dihapus dari server. Kamu bisa menempelkannya lagi kapan saja.",
      tombolYa: "Putuskan",
      bahaya: true,
    });
    if (!setuju) return;
    setSibuk(p.id);
    try {
      await apiFetch(`/api/sosmed/accounts/${a.id}`, { method: "DELETE" });
      setDipilih((lama) => lama.filter((x) => x !== p.id));
      await muatAkun();
    } finally {
      setSibuk("");
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
    if (dipilih.length === 0) return "Centang dulu minimal satu platform tujuan.";
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

  const terhubung = PLATFORM.filter((p) => akunDari(p.id)).length;

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
            <span className={styles.headStatus}>
              {terhubung} / {PLATFORM.length}
            </span>
          </div>

          <p className={styles.hint}>
            Tempel Session ID dari cookie peramban tempat kamu sudah login. Tidak ada jendela
            login yang perlu dibuka, dan satu tempelan berlaku sampai kamu keluar dari akun itu.
          </p>

          <div className={styles.daftarAkun}>
            {memuatAkun && [0, 1, 2].map((i) => <div key={i} className={styles.rangka} />)}

            {!memuatAkun &&
              PLATFORM.map((p) => {
                const a = akunDari(p.id);
                const lagi = sibuk === p.id;
                const terbuka = buka === p.id;
                return (
                  <div key={p.id} className={styles.akunBaris}>
                    <div className={styles.akunUtama}>
                      <span
                        className={a ? styles.logo : `${styles.logo} ${styles.logoMati}`}
                        style={a ? { color: WARNA[p.id] } : undefined}
                      >
                        <LogoSosmed platform={p.id} ukuran={22} />
                      </span>
                      <div className={styles.akunTeks}>
                        <strong>{p.nama}</strong>
                        <span className={a ? styles.sambung : styles.putus}>
                          {a ? "Terhubung" : "Belum terhubung"}
                        </span>
                      </div>
                      <div className={styles.akunAksi}>
                        <button
                          type="button"
                          className={styles.btnSekunder}
                          disabled={lagi}
                          onClick={() => {
                            setGalatAkun("");
                            setSsid("");
                            setBuka(terbuka ? "" : p.id);
                          }}
                        >
                          {terbuka ? "Tutup" : a ? "Ganti" : "Hubungkan"}
                        </button>
                        {a && (
                          <button
                            type="button"
                            className={styles.btnBahaya}
                            disabled={lagi}
                            onClick={() => void putuskan(p, a)}
                          >
                            Putuskan
                          </button>
                        )}
                      </div>
                    </div>

                    {terbuka && (
                      <div className={styles.ssidKotak}>
                        <label className={styles.ssidLabel}>
                          <span>Session ID</span>
                          <input
                            autoFocus
                            value={ssid}
                            spellCheck={false}
                            placeholder={p.cookie.join(" | ")}
                            onChange={(e) => setSsid(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void simpanSsid(p);
                            }}
                          />
                        </label>
                        <p className={styles.ssidPetunjuk}>{p.petunjuk}</p>
                        <div className={styles.aksi}>
                          <button
                            type="button"
                            className={styles.btnUtama}
                            disabled={lagi || !ssid.trim()}
                            onClick={() => void simpanSsid(p)}
                          >
                            {lagi ? "Menyimpan..." : "Simpan"}
                          </button>
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            onClick={() => {
                              setBuka("");
                              setSsid("");
                              setGalatAkun("");
                            }}
                          >
                            Batal
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
          </div>

          {galatAkun && (
            <p className={styles.galat} role="alert">
              {galatAkun}
            </p>
          )}

          <p className={styles.catatan}>
            Session ID setara password: siapa pun yang memilikinya bisa memakai akunmu. Ia
            disimpan di basis data di laptop ini, bukan dikirim ke mana-mana.
          </p>
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
                <label
                  className={unggahan ? `${styles.uploadBtn} ${styles.nonaktif}` : styles.uploadBtn}
                >
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
                    onClick={() =>
                      setDipilih(PLATFORM.filter((p) => akunDari(p.id)).map((p) => p.id))
                    }
                  >
                    Pilih semua
                  </button>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    onClick={() => setDipilih([])}
                  >
                    Kosongkan
                  </button>
                </div>
              </div>
              <div className={styles.daftarCek}>
                {PLATFORM.map((p) => {
                  const bisa = !!akunDari(p.id);
                  const aktif = dipilih.includes(p.id);
                  return (
                    <label
                      key={p.id}
                      className={`${styles.cekItem} ${aktif ? styles.cekAktif : ""} ${bisa ? "" : styles.cekMati}`}
                      title={bisa ? "" : "Tempel dulu Session ID-nya di panel kiri"}
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
                      <span className={styles.logoKecil} style={{ color: WARNA[p.id] }}>
                        <LogoSosmed platform={p.id} ukuran={15} />
                      </span>
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
                      {r.url ? (
                        <a href={r.url} target="_blank" rel="noreferrer" className={styles.tautan}>
                          Lihat postingan
                        </a>
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
