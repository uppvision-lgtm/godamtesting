"use client";

/**
 * Panel pemantauan untuk akun developer.
 *
 * Isinya ringkasan pemakaian tiap akun. Sengaja TIDAK memuat isi akun orang:
 * tidak ada password, tidak ada isian formulir, tidak ada Session ID. Yang
 * menentukan boleh-tidaknya membuka ini tetap server; halaman hanya memutuskan
 * perlu-tidaknya menu ini ditampilkan.
 */

import { useCallback, useEffect, useState } from "react";
import styles from "./PanelDeveloper.module.css";
import { useDialog } from "./Dialog";
import { apiFetch } from "./auth";

type Akun = {
  username: string;
  peran: string;
  created_at: string | null;
  last_login_at: string | null;
  jumlah_template: number;
  jumlah_job: number;
  diri_sendiri: boolean;
};

type Ringkasan = {
  jumlah_akun: number;
  template_akun: number;
  template_tamu: number;
  job_akun: number;
  job_tamu: number;
  job_per_status: Record<string, number>;
};

function waktu(nilai: string | null): string {
  if (!nilai) return "belum pernah";
  const d = new Date(nilai);
  if (Number.isNaN(d.getTime())) return "—";
  const lewat = Date.now() - d.getTime();
  const menit = Math.floor(lewat / 60000);
  if (menit < 1) return "baru saja";
  if (menit < 60) return `${menit} menit lalu`;
  const jam = Math.floor(menit / 60);
  if (jam < 24) return `${jam} jam lalu`;
  const hari = Math.floor(jam / 24);
  if (hari < 30) return `${hari} hari lalu`;
  return d.toLocaleDateString("id-ID", { day: "numeric", month: "short", year: "numeric" });
}

export default function PanelDeveloper({ onTutup }: { onTutup: () => void }) {
  const [akun, setAkun] = useState<Akun[]>([]);
  const [ringkasan, setRingkasan] = useState<Ringkasan | null>(null);
  const [memuat, setMemuat] = useState(true);
  const [galat, setGalat] = useState("");
  const [sibuk, setSibuk] = useState("");
  const dialog = useDialog();

  // Sengaja tidak memasang keadaan apa pun sebelum permintaan dikirim:
  // memanggilnya dari dalam effect akan jadi setState langsung di dalam
  // effect. Rangka pemuatan sudah tampil karena `memuat` mulai dari true,
  // dan penyegaran setelah ubah peran punya penanda sibuk per baris.
  const muat = useCallback(async () => {
    try {
      const res = await apiFetch("/api/admin/users", { cache: "no-store" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Gagal memuat data akun.");
      }
      const data = await res.json();
      setAkun(data.akun || []);
      setRingkasan(data.ringkasan || null);
      setGalat("");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memuat data akun.");
    } finally {
      setMemuat(false);
    }
  }, []);

  useEffect(() => {
    // Dibungkus fungsi async, sama seperti halaman lain di proyek ini: dengan
    // begitu tidak ada pemasangan keadaan yang terjadi langsung saat effect
    // dijalankan, semuanya menyusul setelah jawaban server datang.
    void (async () => {
      await muat();
    })();
  }, [muat]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onTutup();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onTutup]);

  async function ubahPeran(u: Akun) {
    const baru = u.peran === "developer" ? "pengguna" : "developer";
    setSibuk(u.username);
    setGalat("");
    try {
      const res = await apiFetch(
        `/api/admin/users/${encodeURIComponent(u.username)}/peran?peran=${baru}`,
        { method: "POST" }
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Gagal mengubah peran.");
      }
      await muat();
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal mengubah peran.");
    } finally {
      setSibuk("");
    }
  }

  /** Hapus akun orang lain beserta seluruh isinya. Tidak bisa dibatalkan.
   *
   *  Namanya harus diketik ulang. Menghapus akun orang lain membuang
   *  template, video, akun sosmed, dan riwayatnya sekaligus - terlalu berat
   *  untuk dijalankan dari satu klik yang salah sasaran.
   */
  async function hapusAkun(u: Akun) {
    const ketikan = await dialog.tanya({
      judul: `Hapus akun @${u.username}?`,
      pesan:
        `Seluruh isinya ikut dibuang: ${u.jumlah_template} template, ` +
        `${u.jumlah_job} video, akun sosmed yang ditautkan, dan riwayat unggahannya. ` +
        "Ini tidak bisa dibatalkan.",
      label: `Ketik "${u.username}" untuk menghapus`,
      nilaiAwal: "",
      tombolYa: "Hapus permanen",
      bahaya: true,
    });
    if (ketikan === null) return;
    if (ketikan.trim().toLowerCase() !== u.username.toLowerCase()) {
      setGalat("Nama yang diketik tidak cocok; akun tidak jadi dihapus.");
      return;
    }
    setSibuk(u.username);
    setGalat("");
    try {
      const res = await apiFetch(`/api/admin/users/${encodeURIComponent(u.username)}`, {
        method: "DELETE",
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.detail || "Gagal menghapus akun.");
      await muat();
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal menghapus akun.");
    } finally {
      setSibuk("");
    }
  }

  return (
    <div className={styles.latar} onMouseDown={onTutup}>
      {dialog.simpul}
      <div
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label="Pantau akun"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={styles.kepala}>
          <h2 className={styles.judul}>Pantau Akun</h2>
          <button type="button" className={styles.tutup} onClick={onTutup} aria-label="Tutup">
            ×
          </button>
        </div>

        {ringkasan && (
          <div className={styles.angka}>
            <span><b>{ringkasan.jumlah_akun}</b> akun</span>
            <span><b>{ringkasan.template_akun}</b> template akun</span>
            <span><b>{ringkasan.template_tamu}</b> template tamu</span>
            <span><b>{ringkasan.job_akun + ringkasan.job_tamu}</b> video tersimpan</span>
          </div>
        )}

        {galat && <p className={styles.galat} role="alert">{galat}</p>}

        {memuat ? (
          <div className={styles.rangkaBungkus}>
            {[0, 1, 2].map((i) => (
              <div key={i} className={styles.rangka} />
            ))}
          </div>
        ) : akun.length === 0 ? (
          <p className={styles.kosong}>Belum ada akun terdaftar.</p>
        ) : (
          <div className={styles.gulung}>
            <table className={styles.tabel}>
              <thead>
                <tr>
                  <th>Akun</th>
                  <th>Terakhir masuk</th>
                  <th>Dibuat</th>
                  <th className={styles.kanan}>Template</th>
                  <th className={styles.kanan}>Video</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {akun.map((u) => (
                  <tr key={u.username}>
                    <td>
                      <span className={styles.nama}>@{u.username}</span>
                      {u.peran === "developer" && <span className={styles.lencana}>dev</span>}
                      {u.diri_sendiri && <span className={styles.kamu}>kamu</span>}
                    </td>
                    <td className={styles.redup}>{waktu(u.last_login_at)}</td>
                    <td className={styles.redup}>{waktu(u.created_at)}</td>
                    <td className={styles.kanan}>{u.jumlah_template}</td>
                    <td className={styles.kanan}>{u.jumlah_job}</td>
                    <td className={styles.kanan}>
                      {!u.diri_sendiri && (
                        <span className={styles.aksiBaris}>
                          <button
                            type="button"
                            className={styles.peranBtn}
                            disabled={sibuk === u.username}
                            onClick={() => void ubahPeran(u)}
                          >
                            {sibuk === u.username
                              ? "..."
                              : u.peran === "developer"
                                ? "Cabut dev"
                                : "Jadikan dev"}
                          </button>
                          <button
                            type="button"
                            className={styles.hapusAkunBtn}
                            disabled={sibuk === u.username}
                            title={`Hapus akun @${u.username} beserta isinya`}
                            onClick={() => void hapusAkun(u)}
                          >
                            Hapus
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className={styles.catatan}>
          Yang ditampilkan hanya pemakaian. Password, isian formulir, dan Session ID
          milik akun lain tidak bisa dilihat dari sini. Menghapus akun membuang
          seluruh isinya dan tidak bisa dibatalkan; akunmu sendiri dihapus lewat
          menu akun, bukan dari sini.
        </p>
      </div>
    </div>
  );
}
