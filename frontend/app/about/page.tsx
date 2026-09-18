import Link from "next/link";
import styles from "./about.module.css";

/**
 * Panduan lengkap. Bedanya dengan panel "Cara Pakai" di beranda: di sini
 * tiap alat dapat penjelasan utuh beserta hal-hal yang sering bikin
 * bingung (sesi mati, batas caption, platform yang tidak bisa dijadwalkan).
 */

type Bagian = { judul: string; untuk: string; href: string; isi: React.ReactNode[] };

const BAGIAN: Bagian[] = [
  {
    judul: "1. Kelola Akun",
    untuk: "Tempat semua Session ID. Alat lain mengambil akunnya dari sini.",
    href: "/kelola-akun",
    isi: [
      <>
        Login ke sosmednya di browser biasa, lalu tekan <strong>F12</strong> →{" "}
        <strong>Application</strong> → <strong>Cookies</strong> → pilih situsnya, dan
        salin nilai cookie-nya. Password tidak pernah diminta.
      </>,
      <>
        Cookie yang dibutuhkan: Instagram, TikTok, dan Threads{" "}
        <code className={styles.code}>sessionid</code>; X{" "}
        <code className={styles.code}>auth_token</code>; Facebook{" "}
        <code className={styles.code}>c_user</code> dan{" "}
        <code className={styles.code}>xs</code>; YouTube beberapa cookie sekaligus
        (kalau tetap merah, isi juga tiga cookie tambahan di bawahnya).
      </>,
      <>
        Setelah disimpan, sesinya diperiksa dengan membuka beranda akun itu. Tanda
        hijau berarti hidup, merah berarti cookie-nya sudah mati dan harus ditempel
        ulang — akun merah tidak ikut dipakai alat lain. Session ID disimpan
        tersandi di server dan tidak pernah ditampilkan utuh lagi, hanya pangkal
        dan ujungnya.
      </>,
      <>
        Satu platform boleh diisi banyak akun. Alur unggah Facebook dan YouTube
        masih ditandai <strong>belum teruji</strong>: bisa dicoba, tapi belum pernah
        dibuktikan sampai postingannya benar-benar terbit.
      </>,
    ],
  },
  {
    judul: "2. Auto Komen & Like Instagram",
    untuk: "Berkomentar dan menyukai postingan terbaru sebuah akun.",
    href: "/auto-comment",
    isi: [
      <>
        Pilih akun di kotak <strong>Akun</strong> — isinya diambil dari Kelola Akun.
        Satu bot memakai satu akun; Session ID-nya diambil server, jadi tidak perlu
        diketik di halaman ini.
      </>,
      <>
        Isi <strong>Channel</strong> (akun yang mau dikomentari), jumlah komentar per
        postingan (1–100), berapa postingan terbaru yang disasar (1–50), dan jenis
        komentarnya: positif, netral, atau negatif. Komentar negatif tetap dijaga
        sopan — kritik ke isi kontennya, tanpa hinaan, SARA, atau hoaks.
      </>,
      <>
        Jumlah bot bisa dinaikkan sampai <strong>50</strong>, masing-masing dengan
        akun dan sasaran sendiri, lalu dijalankan bersama dari satu tombol{" "}
        <strong>Mulai</strong>.
      </>,
      <>
        Panel <strong>Chrome Live</strong> menampilkan layar bot secara langsung dan
        panel <strong>Log</strong> menampilkan langkah yang sedang dikerjakan.
        Postingan yang disematkan (pin) dilewati. Bot berhenti sendiri setelah
        selesai; untuk berhenti lebih awal tekan <strong>Stop</strong> di kartunya.
      </>,
    ],
  },
  {
    judul: "3. Auto Edit Video",
    untuk: "Menempel video sumber ke template layer buatanmu sendiri.",
    href: "/edit-video",
    isi: [
      <>
        Di panel <strong>Template</strong> disusun lapisan yang menempel di tiap
        video: gambar atau video overlay, intro, outro, posisi kotak tulisan berita,
        dan badge kategori. Template bisa diduplikat, jadi tiap akun boleh punya
        tampilannya sendiri.
      </>,
      <>
        Sumbernya dua pilihan. <strong>Link</strong>: tempel alamat Instagram,
        TikTok, atau YouTube — judul, sampul, dan captionnya langsung terbaca.{" "}
        <strong>File</strong>: unggah video dari komputer lalu tulis captionnya
        sendiri. Link Instagram butuh akun Instagram yang tertaut untuk dibaca.
      </>,
      <>
        Tulisan di video bisa dibuat <strong>AI</strong> (meringkas caption jadi satu
        kalimat berita) atau <strong>Manual</strong> (kata pembuka + isi, maksimal 4
        baris, huruf kecil otomatis jadi kapital). Warnanya hitam atau putih
        mengikuti kotak di templatemu, dan nama pemilik video ditulis sebagai kredit{" "}
        <strong>SUMBER:</strong> di bar bawah.
      </>,
      <>
        Centang template yang dipakai — boleh beberapa sekaligus. Video sumbernya
        diunduh sekali lalu dipakai ulang, jadi sepuluh template tidak berarti
        sepuluh unduhan. Tiap hasil punya batang kemajuannya sendiri, bisa diunduh
        atau langsung dikirim ke Auto Upload.
      </>,
    ],
  },
  {
    judul: "4. Auto Upload",
    untuk: "Satu video, disebar ke banyak akun dan banyak sosmed sekaligus.",
    href: "/auto-upload",
    isi: [
      <>
        Unggah video atau fotonya. Berkas dikirim langsung ke server, jadi ukuran
        besar tidak tertahan di tengah jalan.
      </>,
      <>
        Caption di atas dipakai untuk semua akun; kalau satu akun perlu caption
        berbeda, isi kotak caption khusus di baris akun itu. Batas hurufnya berbeda
        tiap platform — X 280, Threads 500, Instagram dan TikTok 2.200, Facebook dan
        YouTube 5.000.
      </>,
      <>
        Centang akun tujuan. Daftarnya dikelompokkan per platform dan satu platform
        boleh dikirimi beberapa akun sekaligus. Akun yang sesinya sudah mati tidak
        bisa dicentang.
      </>,
      <>
        Kosongkan <strong>Jadwal</strong> untuk mengirim sekarang. Kalau diisi,
        jadwalnya dititipkan ke fitur penjadwalan milik sosmed itu sendiri: TikTok
        (minimal 15 menit dari sekarang, paling jauh 10 hari, menitnya dibulatkan ke
        kelipatan 5) dan X bisa; Instagram, Threads, Facebook, dan YouTube tidak —
        halaman itu menyebutkan alasannya dan meminta platform tersebut dilepas dulu.
      </>,
      <>
        Selama berjalan, kemajuannya tampil dalam persen per akun, dan angka 100%
        hanya muncul kalau prosesnya benar-benar selesai. Tautan hasilnya muncul di{" "}
        <strong>Lihat postingan</strong>, dan riwayatnya tersimpan di bawah.
      </>,
    ],
  },
  {
    judul: "5. Auto Edit dan Upload",
    untuk: "Link sumber masuk, video terbit di semua akun — satu klik.",
    href: "/auto-edit-upload",
    isi: [
      <>
        Tempel link Instagram, TikTok, atau YouTube. Sampul, durasi, ukuran, jumlah
        suka, tanggal, dan caption aslinya langsung tampil sebagai pratinjau.
      </>,
      <>
        Tulisan beritanya disusun otomatis dari caption. Bisa dibuat ulang, diganti
        manual, dan warnanya diatur hitam atau putih. Warna yang dipilih di sini
        hanya berlaku untuk sekali jalan — setelan templatenya sendiri tidak ikut
        berubah.
      </>,
      <>
        <strong>Satu akun, satu template.</strong> Tiap akun tujuan memilih
        templatenya sendiri, jadi sumber videonya sama tapi tampilannya berbeda per
        akun. Memilih tiga template berarti tiga video untuk tiga akun, bukan tiga
        unggahan menumpuk di satu akun.
      </>,
      <>
        Setelah tombolnya ditekan, prosesnya dua langkah: <strong>Edit video</strong>{" "}
        lalu <strong>Upload ke akun</strong>, masing-masing dengan persentase
        sendiri. Akun yang templatenya gagal diedit dilewati dan disebutkan di
        catatan; akun lain tetap jalan.
      </>,
    ],
  },
];

export default function AboutPage() {
  return (
    <main className={styles.page}>
      <Link className={styles.back} href="/">← Kembali</Link>
      <h1 className={styles.title}>Cara Penggunaan</h1>
      <p className={styles.lede}>
        Semua alat di sini masuk ke sosmed memakai <strong>Session ID</strong> —
        cookie dari browser yang sudah login. Tanpa username, tanpa password, dan
        tanpa CAPTCHA. Tempel sekali di <strong>Kelola Akun</strong>, lalu semua alat
        lain tinggal memilih akunnya.
      </p>

      {BAGIAN.map((bagian) => (
        <section key={bagian.judul}>
          <h2 className={styles.title} style={{ fontSize: 22, marginTop: 36 }}>
            {bagian.judul}
          </h2>
          <p className={styles.lede}>{bagian.untuk}</p>
          <ol className={styles.steps}>
            {bagian.isi.map((baris, nomor) => (
              <li key={nomor}>{baris}</li>
            ))}
          </ol>
          <p style={{ marginTop: 12 }}>
            <Link className={styles.back} href={bagian.href}>
              Buka halamannya →
            </Link>
          </p>
        </section>
      ))}

      <p className={styles.warn}>
        Gunakan dengan risiko sendiri. Pastikan aktivitas, komentar, dan unggahan
        mematuhi kebijakan tiap sosial media serta hukum yang berlaku. Jangan
        bagikan Session ID ke siapa pun — siapa pun yang memilikinya bisa masuk ke
        akunmu tanpa password.
      </p>
    </main>
  );
}
