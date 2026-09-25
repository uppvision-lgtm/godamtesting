"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "../auto-upload/page.module.css";
import LogoSosmed, { WARNA, type PlatformId } from "./LogoSosmed";
import { apiFetch } from "./auth";
import { useDialog } from "./Dialog";
import { PLATFORM, type Akun, type Platform } from "./sosmed";

/**
 * Panel akun sosmed: tempel Session ID, periksa, putuskan, tautkan banyak
 * akun per platform.
 *
 * Satu komponen untuk semua halaman. Akunnya tersimpan di server, jadi
 * apa pun yang ditempel di halaman Kelola Akun langsung terlihat di Auto
 * Upload dan Auto Edit & Upload tanpa perlu ditempel ulang.
 */
type Props = {
  judul?: string;
  keterangan?: string;
  /** Rentangkan panel selebar halaman (untuk halaman yang hanya berisi ini). */
  lebar?: boolean;
  /** Dipanggil setiap daftar akun berubah, supaya halaman induk memuat ulang. */
  onBerubah?: () => void;
};

export default function PanelAkun({ judul = "Akun Sosmed", keterangan, lebar, onBerubah }: Props) {
  const dialog = useDialog();

  const [akun, setAkun] = useState<Akun[]>([]);
  const [memuatAkun, setMemuatAkun] = useState(true);
  const [perluLogin, setPerluLogin] = useState(false);
  const [sibuk, setSibuk] = useState<string>("");
  // Platform yang kotak Session ID-nya sedang terbuka. Isiannya dipisah per
  // cookie: Facebook butuh dua cookie dan YouTube tujuh, dan menyatukannya
  // membuat orang salah hitung tanpa tahu di mana letak kesalahannya.
  const [buka, setBuka] = useState<PlatformId | "">("");
  // Baris yang panel rincian Session ID-nya sedang dibentang.
  const [rincian, setRincian] = useState<PlatformId | "">("");
  const [nilaiCookie, setNilaiCookie] = useState<Record<string, string>>({});
  // Nama untuk membedakan akun dalam satu platform. Kosong = "ssid",
  // yaitu akun pertama; nama yang sama akan MENGGANTI akun itu.
  const [namaSesi, setNamaSesi] = useState("");
  const [memeriksa, setMemeriksa] = useState<string>("");
  const [galatAkun, setGalatAkun] = useState("");
  // Cara menautkan akun. "ssid": tempel cookie (bawaan). "sandi": ketik
  // username & password, server yang login lalu mengambil cookienya sendiri;
  // password tidak disimpan. Hasil akhirnya sama: satu baris akun ber-SSID.
  const [metode, setMetode] = useState<"ssid" | "sandi">("ssid");
  const [namaPengguna, setNamaPengguna] = useState("");
  const [sandi, setSandi] = useState("");

  const muatAkun = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/accounts", { cache: "no-store" });
      // 401 = belum login. Session ID setara password, jadi panel ini memang
      // hanya untuk pemilik akun - tetapi diamnya daftar tanpa keterangan
      // membuat orang mengira fiturnya rusak.
      setPerluLogin(res.status === 401);
      if (res.ok) setAkun((await res.json()) as Akun[]);
    } catch {
      // backend sedang mati; daftar dibiarkan seperti adanya
    } finally {
      setMemuatAkun(false);
    }
  }, []);

  useEffect(() => {
    void muatAkun();
  }, [muatAkun]);

  /** SEMUA akun tertaut milik satu platform, urut dari yang paling awal. */
  const akunSemua = (p: PlatformId) =>
    akun.filter((a) => a.platform === p && a.has_session_id).sort((x, y) => x.id - y.id);

  /** Akun terbaru milik satu platform, atau null kalau belum dihubungkan.
   *  Dipilih berdasarkan id TERBESAR, bukan urutan dalam daftar: urutan yang
   *  dikembalikan server tidak boleh jadi andalan. */
  const akunDari = (p: PlatformId) =>
    akun
      .filter((a) => a.platform === p && a.has_session_id)
      .reduce<Akun | null>((pilih, a) => (!pilih || a.id > pilih.id ? a : pilih), null);

  const aktif = PLATFORM.filter((p) => akunDari(p.id)?.verified === true).length;

  const isi = (c: string) => (nilaiCookie[c] || "").trim();

  const lengkap = (p: Platform) => {
    if (!p.cookie.every(isi)) return false;
    const ops = p.cookieOpsional || [];
    const terisi = ops.filter(isi).length;
    // Semua opsional diisi, atau tidak satu pun. Server hanya mengenali dua
    // bentuk itu; jumlah di antaranya ditolaknya sebagai jumlah yang salah.
    return terisi === 0 || terisi === ops.length;
  };

  async function simpanSsid(p: Platform) {
    const kosong = p.cookie.filter((c) => !isi(c));
    if (kosong.length > 0) {
      setGalatAkun(`Masih kosong: ${kosong.join(", ")}.`);
      return;
    }
    const ops = p.cookieOpsional || [];
    const opsTerisi = ops.filter(isi);
    if (opsTerisi.length > 0 && opsTerisi.length < ops.length) {
      setGalatAkun(
        `Cookie tambahan harus diisi semua atau dikosongkan semua. Yang belum: ${ops.filter((c) => !isi(c)).join(", ")}.`
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
          session_name: namaSesi.trim() || "ssid",
          // Server menerimanya sebagai satu untai dipisah "|", urutannya
          // mengikuti daftar cookie tiap platform.
          session_id: [...p.cookie, ...(opsTerisi.length === ops.length ? ops : [])]
            .map(isi)
            .join("|"),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal menyimpan Session ID.");
      setNilaiCookie({});
      setBuka("");
      await muatAkun();
      onBerubah?.();
      // Langsung dibuktikan: beranda akunnya dibuka memakai cookie ini.
      if (data.id) await periksa(p.id, Number(data.id));
    } catch (err) {
      setGalatAkun(err instanceof Error ? err.message : "Gagal menyimpan Session ID.");
    } finally {
      setSibuk("");
    }
  }

  /** Buka beranda akunnya dengan Session ID ini; hijau kalau terbuka. */
  async function masukSandi(p: Platform) {
    if (!namaPengguna.trim() || !sandi) {
      setGalatAkun("Username dan password wajib diisi.");
      return;
    }
    setSibuk(p.id);
    setGalatAkun("");
    try {
      const res = await apiFetch("/api/sosmed/accounts/masuk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: p.id,
          display_name: p.nama,
          session_name: namaSesi.trim() || "ssid",
          username: namaPengguna.trim(),
          password: sandi,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal masuk.");
      setNamaPengguna("");
      setSandi("");
      setBuka("");
      await muatAkun();
      onBerubah?.();
      if (data.id) await periksa(p.id, Number(data.id));
    } catch (err) {
      setGalatAkun(err instanceof Error ? err.message : "Gagal masuk.");
    } finally {
      // Password tidak pernah ditahan di halaman lebih lama dari perlu.
      setSandi("");
      setSibuk("");
    }
  }

  async function periksa(platform: PlatformId, id: number) {
    setMemeriksa(platform);
    try {
      const res = await apiFetch(`/api/sosmed/accounts/${id}/verify`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setGalatAkun(data.detail || "Gagal memeriksa sesi.");
      await muatAkun();
      onBerubah?.();
    } catch {
      setGalatAkun("Gagal memeriksa sesi.");
    } finally {
      setMemeriksa("");
    }
  }

  async function putuskan(p: Platform, a: Akun) {
    const setuju = await dialog.konfirmasi({
      judul: `Putuskan ${p.nama}${a.session_name !== "ssid" ? ` (${a.session_name})` : ""}?`,
      pesan: "Session ID-nya dihapus dari server. Kamu bisa menempelkannya lagi kapan saja.",
      tombolYa: "Putuskan",
      bahaya: true,
    });
    if (!setuju) return;
    setSibuk(p.id);
    try {
      await apiFetch(`/api/sosmed/accounts/${a.id}`, { method: "DELETE" });
      await muatAkun();
      onBerubah?.();
    } finally {
      setSibuk("");
    }
  }

  return (
    <>
      {dialog.simpul}
      <section className={`${styles.panel} ${lebar ? styles.panelLebar : ""}`}>
        <div className={styles.panelHead}>
          <h2>{judul}</h2>
          <span className={styles.headStatus}>
            {aktif} / {PLATFORM.length} aktif
          </span>
        </div>

        <p className={styles.hint}>
          {keterangan ||
            "Tempel Session ID dari cookie peramban tempat kamu sudah login. Tidak ada jendela " +
              "login yang perlu dibuka, dan satu tempelan berlaku sampai kamu keluar dari akun itu."}
        </p>

        <div className={styles.aksi}>
          <button
            type="button"
            className={metode === "ssid" ? styles.btnUtama : styles.btnSekunder}
            onClick={() => {
              setMetode("ssid");
              setGalatAkun("");
            }}
          >
            SSID
          </button>
          <button
            type="button"
            className={metode === "sandi" ? styles.btnUtama : styles.btnSekunder}
            onClick={() => {
              setMetode("sandi");
              setGalatAkun("");
            }}
          >
            Username &amp; Password
          </button>
        </div>
        {metode === "sandi" && (
          <p className={styles.hint}>
            Ketik username dan password akunnya; server login sekali lalu menyimpan cookie
            sesinya — password tidak disimpan. Akun dengan verifikasi dua langkah, atau
            YouTube, tetap harus lewat SSID.
          </p>
        )}

        <div className={styles.daftarAkun}>
          {memuatAkun && [0, 1, 2].map((i) => <div key={i} className={styles.rangka} />)}

          {perluLogin && (
            <p className={styles.pesan}>
              Masuk dulu untuk mengelola akun sosmed. Session ID setara password akunmu, jadi
              bagian ini hanya bisa dipakai setelah login.
            </p>
          )}

          {!memuatAkun &&
            !perluLogin &&
            PLATFORM.map((p) => {
              const a = akunDari(p.id);
              const lagi = sibuk === p.id;
              const terbuka = buka === p.id;
              const dibentang = rincian === p.id && akunSemua(p.id).length > 0;
              return (
                <div key={p.id} className={styles.akunBaris}>
                  <div className={styles.akunUtama}>
                    <button
                      type="button"
                      className={`${styles.panah} ${dibentang ? styles.panahBuka : ""}`}
                      aria-expanded={dibentang}
                      aria-label={`${dibentang ? "Tutup" : "Lihat"} Session ID ${p.nama}`}
                      title={a ? "Lihat Session ID yang tersimpan" : "Belum ada Session ID"}
                      disabled={!a}
                      onClick={() => setRincian(dibentang ? "" : p.id)}
                    >
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d="M6 9l6 6 6-6" />
                      </svg>
                    </button>
                    <span
                      className={a ? styles.logo : `${styles.logo} ${styles.logoMati}`}
                      style={a ? { color: WARNA[p.id] } : undefined}
                    >
                      <LogoSosmed platform={p.id} ukuran={22} />
                    </span>
                    <div className={styles.akunTeks}>
                      <strong>{p.nama}</strong>
                      <span
                        className={
                          !a
                            ? styles.putus
                            : a.verified === true
                              ? styles.sambung
                              : a.verified === false
                                ? styles.mati
                                : styles.belum
                        }
                        title={a?.verify_message || ""}
                      >
                        {!a
                          ? "Belum terhubung"
                          : memeriksa === p.id
                            ? "Memeriksa beranda akunnya..."
                            : a.verified === true
                              ? a.nama_akun
                                ? `Aktif — ${a.nama_akun}`
                                : "Aktif — beranda akunnya terbuka"
                              : a.verified === false
                                ? a.verify_message || "Sesinya sudah tidak berlaku"
                                : "Tersimpan, belum diperiksa"}
                      </span>
                    </div>
                    <div className={styles.akunAksi}>
                      <button
                        type="button"
                        className={styles.btnSekunder}
                        disabled={lagi}
                        onClick={() => {
                          setGalatAkun("");
                          setNilaiCookie({});
                          setNamaSesi("");
                          setBuka(terbuka ? "" : p.id);
                        }}
                      >
                        {terbuka ? "Tutup" : a ? "Ganti" : "Hubungkan"}
                      </button>
                      {a && !terbuka && (
                        <button
                          type="button"
                          className={styles.btnSekunder}
                          disabled={lagi}
                          title="Tautkan satu akun lagi di platform ini"
                          onClick={() => {
                            setGalatAkun("");
                            setNilaiCookie({});
                            setNamaSesi(`akun-${akunSemua(p.id).length + 1}`);
                            setBuka(p.id);
                          }}
                        >
                          + Akun
                        </button>
                      )}
                      {a && (
                        <>
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            disabled={lagi || memeriksa === p.id}
                            title="Buka beranda akunnya sekali untuk memastikan sesinya masih hidup"
                            onClick={() => void periksa(p.id, a.id)}
                          >
                            {memeriksa === p.id ? "Memeriksa..." : "Periksa"}
                          </button>
                          <button
                            type="button"
                            className={styles.btnBahaya}
                            disabled={lagi}
                            onClick={() => void putuskan(p, a)}
                          >
                            Putuskan
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {akunSemua(p.id).length > 1 && (
                    <div className={styles.daftarSesi}>
                      {akunSemua(p.id).map((s) => (
                        <div key={s.id} className={styles.sesiBaris}>
                          <span className={styles.sesiNama}>{s.session_name}</span>
                          <span
                            className={
                              s.verified === true
                                ? styles.sambung
                                : s.verified === false
                                  ? styles.mati
                                  : styles.belum
                            }
                          >
                            {s.verified === true
                              ? s.nama_akun || "aktif"
                              : s.verified === false
                                ? "sesi mati"
                                : "belum diperiksa"}
                          </span>
                          <button
                            type="button"
                            className={styles.btnSekunder}
                            disabled={lagi || memeriksa === p.id}
                            onClick={() => void periksa(p.id, s.id)}
                          >
                            Periksa
                          </button>
                          <button
                            type="button"
                            className={styles.btnBahaya}
                            disabled={lagi}
                            onClick={() => void putuskan(p, s)}
                          >
                            Putuskan
                          </button>
                        </div>
                      ))}
                    </div>
                  )}

                  {dibentang && (
                    <div className={styles.rincianKotak}>
                      {/* Satu blok per AKUN: platform bisa punya beberapa akun. */}
                      {akunSemua(p.id).map((s) => (
                        <div key={s.id} className={styles.rincianAkun}>
                          <div className={styles.rincianJudul}>
                            <span className={styles.sesiNama}>{s.session_name}</span>
                            <span className={styles.rincianPanjang}>
                              {s.nama_akun || "belum diperiksa"}
                            </span>
                          </div>
                          {s.cookie_terisi.length === 0 ? (
                            <p className={styles.hint}>Rinciannya tidak terbaca.</p>
                          ) : (
                            s.cookie_terisi.map((c) => (
                              <div key={c.nama} className={styles.rincianBaris}>
                                <span className={styles.rincianNama}>{c.nama}</span>
                                <span className={styles.rincianNilai}>{c.pratinjau}</span>
                                <span className={styles.rincianPanjang}>{c.panjang} huruf</span>
                              </div>
                            ))
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {terbuka && metode === "sandi" && (
                    <div className={styles.ssidKotak}>
                      {p.id === "youtube" ? (
                        <p className={styles.ssidPetunjuk}>
                          YouTube tidak bisa dimasuki otomatis dengan password — Google
                          memblokir sign-in dari peramban otomatis. Pakai SSID untuk YouTube.
                        </p>
                      ) : (
                        <>
                          <p className={styles.ssidPetunjuk}>
                            Masuk ke {p.nama} dengan username & password. Password hanya dipakai
                            sekali untuk login, tidak disimpan.
                          </p>
                          <label className={styles.ssidLabel}>
                            <span>
                              nama akun
                              <em className={styles.opsional}>
                                {" "}
                                untuk membedakan beberapa akun di platform yang sama
                              </em>
                            </span>
                            <input
                              value={namaSesi}
                              spellCheck={false}
                              placeholder="mis. akun-2 (kosongkan untuk mengganti akun utama)"
                              onChange={(e) => setNamaSesi(e.target.value)}
                            />
                          </label>
                          <label className={styles.ssidLabel}>
                            <span>username</span>
                            <input
                              autoFocus
                              value={namaPengguna}
                              spellCheck={false}
                              autoComplete="username"
                              placeholder={p.id === "facebook" ? "email atau nomor telepon" : "username atau email"}
                              onChange={(e) => setNamaPengguna(e.target.value)}
                            />
                          </label>
                          <label className={styles.ssidLabel}>
                            <span>password</span>
                            <input
                              type="password"
                              value={sandi}
                              autoComplete="current-password"
                              placeholder="password akun"
                              onChange={(e) => setSandi(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && namaPengguna.trim() && sandi)
                                  void masukSandi(p);
                              }}
                            />
                          </label>
                          <div className={styles.aksi}>
                            <button
                              type="button"
                              className={styles.btnUtama}
                              disabled={lagi || !namaPengguna.trim() || !sandi}
                              onClick={() => void masukSandi(p)}
                            >
                              {lagi ? "Sedang masuk..." : "Masuk & simpan"}
                            </button>
                            <button
                              type="button"
                              className={styles.btnSekunder}
                              onClick={() => {
                                setBuka("");
                                setNamaPengguna("");
                                setSandi("");
                                setNamaSesi("");
                                setGalatAkun("");
                              }}
                            >
                              Batal
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {terbuka && metode === "ssid" && (
                    <div className={styles.ssidKotak}>
                      <p className={styles.ssidPetunjuk}>{p.petunjuk}</p>
                      <label className={styles.ssidLabel}>
                        <span>
                          nama akun
                          <em className={styles.opsional}>
                            {" "}
                            untuk membedakan beberapa akun di platform yang sama
                          </em>
                        </span>
                        <input
                          value={namaSesi}
                          spellCheck={false}
                          placeholder="mis. akun-2 (kosongkan untuk mengganti akun utama)"
                          onChange={(e) => setNamaSesi(e.target.value)}
                        />
                      </label>
                      {/* Satu kotak per cookie: jelas mana yang masih kosong. */}
                      {[...p.cookie, ...(p.cookieOpsional || [])].map((c, i) => (
                        <label key={c} className={styles.ssidLabel}>
                          <span>
                            {c}
                            {(p.cookieOpsional || []).includes(c) && (
                              <em className={styles.opsional}> opsional</em>
                            )}
                          </span>
                          <input
                            autoFocus={i === 0}
                            value={nilaiCookie[c] || ""}
                            spellCheck={false}
                            placeholder={`nilai cookie ${c}`}
                            onChange={(e) =>
                              setNilaiCookie((lama) => ({ ...lama, [c]: e.target.value }))
                            }
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && lengkap(p)) void simpanSsid(p);
                            }}
                          />
                        </label>
                      ))}
                      <div className={styles.aksi}>
                        <button
                          type="button"
                          className={styles.btnUtama}
                          disabled={lagi || !lengkap(p)}
                          onClick={() => void simpanSsid(p)}
                        >
                          {lagi ? "Menyimpan..." : "Simpan & periksa"}
                        </button>
                        <button
                          type="button"
                          className={styles.btnSekunder}
                          onClick={() => {
                            setBuka("");
                            setNilaiCookie({});
                            setNamaSesi("");
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
          Session ID setara password: siapa pun yang memilikinya bisa memakai akunmu. Ia disimpan
          tersandikan di server, dan dipakai oleh semua alat di situs ini tanpa perlu ditempel ulang.
        </p>
      </section>
    </>
  );
}
