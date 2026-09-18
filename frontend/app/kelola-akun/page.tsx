"use client";

import Link from "next/link";
import styles from "../auto-upload/page.module.css";
import LoginButton from "../components/LoginButton";
import PanelAkun from "../components/PanelAkun";

/**
 * Kelola Akun: satu tempat untuk menempel Session ID semua sosmed.
 *
 * Akun yang ditempel di sini dipakai oleh Auto Upload dan Auto Edit & Upload
 * tanpa perlu ditempel ulang - keduanya membaca daftar yang sama dari server.
 */
export default function KelolaAkunPage() {
  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">
          ←
        </Link>
        <span className={styles.brand}>AUTOMASI · KELOLA AKUN</span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        <PanelAkun
          lebar
          judul="Kelola Akun"
          keterangan={
            "Tempel Session ID tiap sosmed sekali di sini. Auto Upload dan Auto Edit & Upload " +
            "langsung memakai akun-akun ini - tidak perlu mengetik Session ID lagi di sana. " +
            "Satu platform boleh punya banyak akun; tekan + Akun untuk menambah."
          }
        />

        <section className={`${styles.panel} ${styles.panelLebar}`}>
          <div className={styles.panelHead}>
            <h2>Dipakai di mana?</h2>
          </div>
          <p className={styles.hint}>
            <Link href="/auto-upload" className={styles.tautan}>
              Auto Upload
            </Link>{" "}
            — pilih akun tujuan dari daftar ini saat mengirim video.
          </p>
          <p className={styles.hint}>
            <Link href="/auto-edit-upload" className={styles.tautan}>
              Auto Edit &amp; Upload
            </Link>{" "}
            — tempel link, videonya diedit lalu dikirim ke akun-akun ini dalam satu klik. Akun
            Instagram di sini juga dipakai untuk membaca link sumber dari Instagram.
          </p>
        </section>
      </main>
    </div>
  );
}
