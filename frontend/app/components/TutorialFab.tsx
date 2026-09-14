"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import styles from "./TutorialFab.module.css";

type Step = { title: string; body: React.ReactNode };

const STEPS: Step[] = [
  {
    title: "Ambil Session ID Instagram",
    body: (
      <>
        Login Instagram di browser biasa, tekan <b>F12</b> →{" "}
        <b>Application</b> → <b>Cookies</b> → <code>https://www.instagram.com</code>, lalu
        salin nilai cookie <code>sessionid</code>. Tidak perlu password.
      </>
    ),
  },
  {
    title: "Isi form botnya",
    body: (
      <>
        Nama akun, Session ID, channel target, jumlah komentar per postingan (1–100),
        jumlah postingan terbaru (1–50), dan jenis komentar:{" "}
        <b>positif</b>, <b>netral</b>, atau <b>negatif</b>. Bisa 1–5 bot sekaligus.
      </>
    ),
  },
  {
    title: "Klik Mulai lalu pantau",
    body: (
      <>
        Panel <b>Chrome Live</b> menampilkan layar bot secara langsung dan panel{" "}
        <b>Log</b> menampilkan langkah yang sedang dikerjakan. Postingan yang
        disematkan (pin) otomatis dilewati.
      </>
    ),
  },
  {
    title: "Selesai atau berhenti",
    body: (
      <>
        Bot berhenti sendiri setelah semua postingan selesai dan browsernya ditutup
        otomatis. Ingin berhenti lebih awal? Tekan <b>Stop</b> pada kartu bot.
      </>
    ),
  },
];

/** Tombol mengambang di kanan bawah: panduan singkat cara memakai website. */
export default function TutorialFab() {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Esc untuk menutup, dan fokus dipindah ke panel saat dibuka.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    panelRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      {open && <div className={styles.backdrop} onClick={() => setOpen(false)} />}

      <div className={styles.wrap}>
        {open && (
          <div
            ref={panelRef}
            className={styles.panel}
            role="dialog"
            aria-modal="true"
            aria-label="Cara penggunaan"
            tabIndex={-1}
          >
            <div className={styles.panelHead}>
              <h2>Cara Pakai</h2>
              <button
                type="button"
                className={styles.closeBtn}
                onClick={() => setOpen(false)}
                aria-label="Tutup panduan"
              >
                ✕
              </button>
            </div>

            <ol className={styles.steps}>
              {STEPS.map((step) => (
                <li key={step.title}>
                  <b className={styles.stepTitle}>{step.title}</b>
                  <span className={styles.stepBody}>{step.body}</span>
                </li>
              ))}
            </ol>

            <p className={styles.warn}>
              Jangan bagikan Session ID ke siapa pun — siapa pun yang memilikinya bisa
              masuk ke akunmu. Pakai dengan bijak dan tanggung jawab sendiri.
            </p>

            <Link className={styles.more} href="/about" onClick={() => setOpen(false)}>
              Panduan lengkap →
            </Link>
          </div>
        )}

        <button
          type="button"
          className={styles.fab}
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <span className={styles.fabIcon} aria-hidden="true">{open ? "✕" : "?"}</span>
          <span className={styles.fabText}>{open ? "Tutup" : "Cara Pakai"}</span>
        </button>
      </div>
    </>
  );
}
