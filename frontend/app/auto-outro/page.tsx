"use client";

import Link from "next/link";
import { useState } from "react";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import { PERAN_DEVELOPER, usePeran, useSudahHidrasi } from "../components/auth";

/**
 * Auto Outro — khusus akun developer.
 *
 * Baru tampilannya: isian dan tombol. Mesin pembuat videonya belum
 * disambungkan, jadi tombol "Buat Outro" hanya menyiapkan dan menampilkan
 * isian yang nanti dikirim. Begitu arahannya turun, tinggal isian yang sama
 * ini yang disambungkan ke server - bentuk halamannya tidak perlu berubah.
 */

type Sosmed = {
  kunci: string;
  nama: string;
  ikon: string;
  contoh: string;
};

// Urutannya sengaja sama dengan yang diminta: X, TikTok, Facebook, YouTube,
// Instagram, Threads.
const SOSMED: Sosmed[] = [
  { kunci: "x", nama: "X", ikon: "𝕏", contoh: "@namaakun" },
  { kunci: "tiktok", nama: "TikTok", ikon: "🎵", contoh: "@namaakun" },
  { kunci: "facebook", nama: "Facebook", ikon: "📘", contoh: "Nama Halaman" },
  { kunci: "youtube", nama: "YouTube", ikon: "▶️", contoh: "@namachannel" },
  { kunci: "instagram", nama: "Instagram", ikon: "📸", contoh: "@namaakun" },
  { kunci: "threads", nama: "Threads", ikon: "🧵", contoh: "@namaakun" },
];

const KOSONG: Record<string, string> = Object.fromEntries(
  SOSMED.map((s) => [s.kunci, ""])
);

export default function AutoOutroPage() {
  const peran = usePeran();
  const siap = useSudahHidrasi();
  const [channel, setChannel] = useState("");
  const [akun, setAkun] = useState<Record<string, string>>(KOSONG);
  const [terkirim, setTerkirim] = useState<{
    channel: string;
    akun: Record<string, string>;
  } | null>(null);

  const bolehLihat = peran === PERAN_DEVELOPER;
  const bisaJalan = channel.trim().length > 0;

  function ubahAkun(kunci: string, nilai: string) {
    setAkun((lama) => ({ ...lama, [kunci]: nilai }));
  }

  function jalankan() {
    if (!bisaJalan) return;
    setTerkirim({ channel: channel.trim(), akun: { ...akun } });
  }

  function kosongkan() {
    setChannel("");
    setAkun(KOSONG);
    setTerkirim(null);
  }

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
        {/* Sebelum localStorage terbaca, peran belum diketahui. Menampilkan
            penolakan lebih dulu akan berkedip di mata developer sendiri. */}
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
              <p className={styles.hint}>
                Isi nama channel dan nama akun tiap sosmed, lalu tekan sekali — outronya
                dibuat sendiri.
              </p>

              <div className={styles.form}>
                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama channel</h3>
                  </div>
                  <div className={styles.row}>
                    <label>
                      <span>Nama channel</span>
                      <input
                        type="text"
                        value={channel}
                        onChange={(e) => setChannel(e.target.value)}
                        placeholder="TV Rakyat"
                        maxLength={80}
                      />
                    </label>
                  </div>
                </div>

                <div className={styles.blok}>
                  <div className={styles.blokHead}>
                    <h3>Nama akun tiap sosmed</h3>
                  </div>
                  <p className={styles.hint}>
                    Yang dikosongkan tidak akan ditampilkan di outronya.
                  </p>
                  {SOSMED.map((s) => (
                    <div key={s.kunci} className={styles.row}>
                      <label>
                        <span>
                          {s.ikon} {s.nama}
                        </span>
                        <input
                          type="text"
                          value={akun[s.kunci] || ""}
                          onChange={(e) => ubahAkun(s.kunci, e.target.value)}
                          placeholder={s.contoh}
                          maxLength={80}
                        />
                      </label>
                    </div>
                  ))}
                </div>

                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnUtama}
                    onClick={jalankan}
                    disabled={!bisaJalan}
                    title={bisaJalan ? "Buat outro" : "Isi nama channel dulu"}
                  >
                    Buat Outro
                  </button>
                  <button type="button" className={styles.btnSekunder} onClick={kosongkan}>
                    Kosongkan
                  </button>
                </div>
              </div>
            </section>

            <section className={`${styles.panel} ${styles.panelLebar}`}>
              <div className={styles.panelHead}>
                <h2>Hasil</h2>
              </div>
              {terkirim === null ? (
                <p className={styles.hint}>
                  Videonya akan muncul di sini setelah tombol ditekan.
                </p>
              ) : (
                <>
                  <p className={styles.peringatan}>
                    Mesin pembuat outronya belum disambungkan — isian dan tombolnya sudah
                    siap, tinggal menunggu arahan berikutnya.
                  </p>
                  <div className={styles.rincianKotak}>
                    <div className={styles.rincianBaris}>
                      <span className={styles.rincianNama}>Nama channel</span>
                      <span className={styles.rincianNilai}>{terkirim.channel}</span>
                    </div>
                    {SOSMED.map((s) => (
                      <div key={s.kunci} className={styles.rincianBaris}>
                        <span className={styles.rincianNama}>
                          {s.ikon} {s.nama}
                        </span>
                        <span className={styles.rincianNilai}>
                          {terkirim.akun[s.kunci]?.trim() || "—"}
                        </span>
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
