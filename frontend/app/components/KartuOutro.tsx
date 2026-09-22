"use client";

import Link from "next/link";
import styles from "../page.module.css";
import { PERAN_DEVELOPER, usePeran, useSudahHidrasi } from "./auth";

/**
 * Kartu "Auto Outro" di beranda — hanya muncul untuk akun developer.
 *
 * Halaman beranda dirender di server dan tidak tahu siapa yang membuka, jadi
 * kartu ini dipisah sebagai komponen browser. Yang menentukan boleh-tidaknya
 * tetap server pada setiap permintaan; ini hanya soal tampil atau tidak.
 */
export default function KartuOutro() {
  const peran = usePeran();
  const siap = useSudahHidrasi();
  if (!siap || peran !== PERAN_DEVELOPER) return null;
  return (
    <Link href="/auto-outro" className={styles.card}>
      <span className={styles.cardTop}>
        <span className={styles.icon}>🎞️</span>
        <span className={styles.badge}>Developer</span>
      </span>
      <h2>Auto Outro</h2>
      <p>isi nama channel dan sosmednya, outronya dibuat sendiri dalam satu klik</p>
      <span className={styles.cta}>
        Buka
        <span className={styles.arrow}>→</span>
      </span>
    </Link>
  );
}
