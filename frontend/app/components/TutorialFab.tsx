"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import styles from "./TutorialFab.module.css";

/**
 * Panduan singkat "Cara Pakai" untuk SEMUA alat, bukan cuma Auto Komen.
 *
 * Isinya sengaja ditaruh di satu tempat dan dipilih lewat tab, karena panel
 * mengambang ini sempit: menumpuk lima panduan sekaligus membuatnya jadi
 * gulungan panjang yang tidak ada yang membaca.
 */

type Langkah = { judul: string; isi: React.ReactNode };

type Panduan = {
  id: string;
  ikon: string;
  nama: string;
  /** Kalimat pembuka: alat ini untuk apa. */
  untuk: string;
  href: string;
  langkah: Langkah[];
};

const PANDUAN: Panduan[] = [
  {
    id: "akun",
    ikon: "🔑",
    nama: "Kelola Akun",
    untuk: "Mulai dari sini. Tempel Session ID sekali, dipakai semua alat lain.",
    href: "/kelola-akun",
    langkah: [
      {
        judul: "Ambil cookie-nya dari browser",
        isi: (
          <>
            Login di browser biasa, tekan <b>F12</b> → <b>Application</b> →{" "}
            <b>Cookies</b> → pilih situsnya, lalu salin nilainya. Instagram, TikTok,
            dan Threads memakai <code>sessionid</code>; X memakai{" "}
            <code>auth_token</code>; Facebook <code>c_user</code> + <code>xs</code>;
            YouTube beberapa cookie sekaligus. Password tidak pernah diminta.
          </>
        ),
      },
      {
        judul: "Tempel lalu simpan",
        isi: (
          <>
            Pilih platformnya, tempel ke kotak yang namanya sama, beri nama akun
            supaya mudah dikenali, lalu <b>Simpan</b>. Satu platform boleh diisi
            banyak akun.
          </>
        ),
      },
      {
        judul: "Pastikan tandanya hijau",
        isi: (
          <>
            Setelah disimpan, sesinya diperiksa dengan membuka beranda akun itu.
            Hijau = hidup, merah = cookie-nya sudah mati dan perlu ditempel ulang.
            Akun merah tidak ikut dipakai alat lain.
          </>
        ),
      },
      {
        judul: "Tidak perlu diketik lagi",
        isi: (
          <>
            Auto Komen, Auto Upload, dan Auto Edit &amp; Upload mengambil akunnya
            dari sini. Session ID-nya disimpan tersandi di server dan tidak pernah
            ditampilkan utuh lagi.
          </>
        ),
      },
    ],
  },
  {
    id: "komen",
    ikon: "💬",
    nama: "Auto Komen",
    untuk: "Berkomentar dan menyukai postingan terbaru sebuah akun, otomatis.",
    href: "/auto-comment",
    langkah: [
      {
        judul: "Pilih akunnya",
        isi: (
          <>
            Di kotak <b>Akun</b> pilih salah satu akun Instagram yang sudah tertaut.
            Satu bot memakai satu akun. Belum ada isinya? Tautkan dulu di{" "}
            <b>Kelola Akun</b>.
          </>
        ),
      },
      {
        judul: "Isi sasaran dan takarannya",
        isi: (
          <>
            <b>Channel</b> = akun yang mau dikomentari. Lalu jumlah komentar per
            postingan (1–100), berapa postingan terbaru yang disasar (1–50), dan
            jenis komentarnya: <b>positif</b>, <b>netral</b>, atau <b>negatif</b>.
          </>
        ),
      },
      {
        judul: "Bisa banyak bot sekaligus",
        isi: (
          <>
            Jumlah bot bisa dinaikkan sampai <b>50</b> lewat tombol pintas atau kotak
            angka di sebelahnya. Tiap bot punya akun dan sasaran sendiri, dan semuanya
            berjalan dari satu tombol <b>Mulai</b>.
          </>
        ),
      },
      {
        judul: "Pantau, lalu selesai",
        isi: (
          <>
            Panel <b>Chrome Live</b> menampilkan layar bot secara langsung dan panel{" "}
            <b>Log</b> menampilkan langkah yang sedang dikerjakan. Postingan yang
            disematkan (pin) dilewati. Bot berhenti sendiri saat selesai; mau lebih
            awal, tekan <b>Stop</b> di kartunya.
          </>
        ),
      },
    ],
  },
  {
    id: "edit",
    ikon: "🎬",
    nama: "Auto Edit Video",
    untuk: "Menempel video sumber ke template layer-mu sendiri.",
    href: "/edit-video",
    langkah: [
      {
        judul: "Siapkan templatenya",
        isi: (
          <>
            Di panel <b>Template</b>, susun lapisan yang menempel di tiap video:
            gambar/video overlay, intro, outro, kotak tulisan berita, dan badge
            kategori. Template bisa diduplikat, jadi tiap akun boleh punya
            tampilannya sendiri.
          </>
        ),
      },
      {
        judul: "Masukkan videonya",
        isi: (
          <>
            Mode <b>Link</b>: tempel link Instagram, TikTok, atau YouTube — judul,
            sampul, dan captionnya langsung terbaca. Mode <b>File</b>: unggah video
            dari komputer, lalu tulis captionnya sendiri.
          </>
        ),
      },
      {
        judul: "Atur tulisan di video",
        isi: (
          <>
            Mode <b>AI</b> meringkas caption jadi satu kalimat berita; mode{" "}
            <b>Manual</b> kamu ketik sendiri (kata pembuka + isi, maksimal 4 baris).
            Warna tulisan <b>Hitam/Putih</b> mengikuti kotak di templatemu, dan nama
            pemilik video ditulis sebagai kredit <b>SUMBER:</b> di bar bawah.
          </>
        ),
      },
      {
        judul: "Render lalu unduh",
        isi: (
          <>
            Centang template yang dipakai — boleh beberapa sekaligus, videonya
            diunduh sekali lalu dipakai ulang. Setiap hasil punya batang kemajuan
            sendiri dan bisa diunduh atau langsung dikirim ke Auto Upload.
          </>
        ),
      },
    ],
  },
  {
    id: "upload",
    ikon: "⬆️",
    nama: "Auto Upload",
    untuk: "Satu video, disebar ke banyak akun dan banyak sosmed sekaligus.",
    href: "/auto-upload",
    langkah: [
      {
        judul: "Pilih berkasnya",
        isi: (
          <>
            Unggah video atau foto dari komputer. Berkasnya dikirim langsung ke
            server, jadi ukuran besar pun tidak tertahan.
          </>
        ),
      },
      {
        judul: "Tulis caption",
        isi: (
          <>
            Caption di atas dipakai untuk semua akun. Kalau satu akun perlu caption
            berbeda, isi kotak caption khusus di baris akun itu. Batas hurufnya
            berbeda tiap platform — X cuma 280, Threads 500.
          </>
        ),
      },
      {
        judul: "Centang akun tujuan",
        isi: (
          <>
            Daftarnya diambil dari <b>Kelola Akun</b>, dikelompokkan per platform.
            Satu platform boleh dikirimi beberapa akun sekaligus. Akun yang sesinya
            mati tidak bisa dicentang.
          </>
        ),
      },
      {
        judul: "Sekarang atau dijadwalkan",
        isi: (
          <>
            Kosongkan <b>Jadwal</b> untuk mengirim sekarang. Kalau diisi, jadwalnya
            dititipkan ke fitur penjadwalan milik sosmed itu sendiri — TikTok dan X
            bisa, Instagram dan Threads tidak, dan halaman itu memberi tahu mana yang
            harus dilepas. Kemajuannya muncul dalam persen, dan tautan hasilnya
            tampil di <b>Lihat postingan</b>.
          </>
        ),
      },
    ],
  },
  {
    id: "rantai",
    ikon: "⚡",
    nama: "Edit & Upload",
    untuk: "Link sumber masuk, video terbit di semua akun — satu klik.",
    href: "/auto-edit-upload",
    langkah: [
      {
        judul: "Tempel link sumber",
        isi: (
          <>
            Link Instagram, TikTok, atau YouTube. Sampul, durasi, jumlah suka, dan
            caption aslinya langsung tampil sebagai pratinjau.
          </>
        ),
      },
      {
        judul: "Periksa teks di video",
        isi: (
          <>
            Tulisan beritanya disusun otomatis dari caption — bisa dibuat ulang,
            diganti manual, dan warnanya diatur <b>Hitam/Putih</b>. Warna yang kamu
            tekan di sini hanya berlaku untuk sekali jalan; setelan templatenya tidak
            berubah.
          </>
        ),
      },
      {
        judul: "Satu akun, satu template",
        isi: (
          <>
            Tiap akun tujuan memilih templatenya sendiri, jadi sumbernya sama tapi
            tampilannya berbeda per akun. Memilih tiga template berarti tiga video
            untuk tiga akun, bukan tiga unggahan di satu akun.
          </>
        ),
      },
      {
        judul: "Jalankan sekali",
        isi: (
          <>
            Tekan tombolnya, lalu proses berjalan dua langkah: <b>Edit video</b>{" "}
            kemudian <b>Upload ke akun</b>, masing-masing dengan persentasenya
            sendiri. Akun yang templatenya gagal diedit dilewati dan disebut di
            catatan, sisanya tetap jalan.
          </>
        ),
      },
    ],
  },
];

/** Tombol mengambang di kanan bawah: panduan singkat cara memakai website. */
export default function TutorialFab() {
  const [open, setOpen] = useState(false);
  const [aktif, setAktif] = useState(PANDUAN[0].id);
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

  const panduan = PANDUAN.find((p) => p.id === aktif) || PANDUAN[0];

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

            <div className={styles.tabs} role="tablist" aria-label="Pilih alat">
              {PANDUAN.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  role="tab"
                  aria-selected={p.id === aktif}
                  className={p.id === aktif ? styles.tabAktif : styles.tab}
                  onClick={() => {
                    setAktif(p.id);
                    // Panel ini bisa panjang dan bergulir sendiri. Pindah tab
                    // harus mengembalikannya ke paling atas, supaya judul, tab,
                    // dan langkah pertama yang terlihat - bukan tengah-tengah
                    // panduan sebelumnya.
                    panelRef.current?.scrollTo({ top: 0 });
                  }}
                >
                  <span aria-hidden="true">{p.ikon}</span> {p.nama}
                </button>
              ))}
            </div>

            <div>
              <p className={styles.untuk}>{panduan.untuk}</p>

              <ol className={styles.steps}>
                {panduan.langkah.map((langkah) => (
                  <li key={langkah.judul}>
                    <b className={styles.stepTitle}>{langkah.judul}</b>
                    <span className={styles.stepBody}>{langkah.isi}</span>
                  </li>
                ))}
              </ol>

              <Link
                className={styles.buka}
                href={panduan.href}
                onClick={() => setOpen(false)}
              >
                Buka {panduan.nama} →
              </Link>
            </div>

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
