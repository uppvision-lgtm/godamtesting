/**
 * Definisi bersama akun sosmed: tipe akun dari server dan konfigurasi tiap
 * platform (nama, batas caption, cookie yang wajib ditempel, kemampuan
 * jadwal).
 *
 * Dipakai oleh halaman Kelola Akun, Auto Upload, dan Auto Edit & Upload.
 * Sengaja satu berkas: kalau tiap halaman menyalin daftarnya sendiri,
 * menambah satu cookie di satu tempat berarti tiga halaman bisa berbeda.
 */

import type { PlatformId } from "./LogoSosmed";

export type Akun = {
  id: number;
  platform: PlatformId;
  display_name: string;
  session_name: string;
  has_session_id: boolean;
  has_browser_session: boolean;
  /** null = belum pernah diperiksa. true = beranda akunnya terbuka. */
  verified: boolean | null;
  verified_at: string | null;
  verify_message: string;
  /** Nama akun yang terbaca dari profil saat pemeriksaan terakhir. */
  nama_akun: string;
  /** Ringkasan Session ID per cookie, SUDAH disamarkan server. */
  cookie_terisi: { nama: string; pratinjau: string; panjang: number }[];
};

export type Platform = {
  id: PlatformId;
  nama: string;
  batas: number;
  cookie: string[];
  /** Cookie tambahan yang boleh dikosongkan. Server hanya menerima semuanya
   *  atau tidak sama sekali. */
  cookieOpsional?: string[];
  petunjuk: string;
  /** Alur unggahnya belum pernah diuji sampai terbit. */
  belumTeruji?: boolean;
  /** Punya fitur penjadwalan sendiri di komposer webnya. */
  bisaJadwal?: boolean;
  /** Kenapa tidak bisa dijadwalkan. Wajib diisi kalau bisaJadwal false. */
  alasanJadwal?: string;
  /** Batasan penjadwalan pada platform yang BISA dijadwalkan. */
  catatanJadwal?: string;
  /** Jeda minimum dari sekarang, dalam menit. */
  jedaMinimalMenit?: number;
  /** Batas terjauh jadwal, dalam hari. */
  jedaMaksimalHari?: number;
};

export const PLATFORM: Platform[] = [
  {
    id: "instagram",
    nama: "Instagram",
    batas: 2200,
    cookie: ["sessionid"],
    petunjuk: "Buka instagram.com, lalu salin nilai cookie sessionid.",
    bisaJadwal: false,
    alasanJadwal:
      "komposer webnya tidak punya pilihan jadwal; menjadwalkan Instagram butuh " +
      "akun profesional lewat Meta Business Suite",
  },
  {
    id: "tiktok",
    nama: "TikTok",
    batas: 2200,
    cookie: ["sessionid"],
    petunjuk: "Buka tiktok.com, lalu salin nilai cookie sessionid.",
    bisaJadwal: true,
    jedaMinimalMenit: 15,
    jedaMaksimalHari: 10,
    catatanJadwal:
      "jadwalnya minimal 15 menit dari sekarang, paling jauh 10 hari ke depan, " +
      "dan menitnya dibulatkan ke kelipatan 5",
  },
  {
    id: "youtube",
    nama: "YouTube",
    batas: 5000,
    cookie: ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"],
    cookieOpsional: ["__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO"],
    petunjuk:
      "Buka youtube.com, salin cookie-nya satu per satu. Google sering menolak " +
      "sesi tanpa ketiga cookie tambahan di bawah, jadi isi juga kalau YouTube " +
      "tetap merah.",
    belumTeruji: true,
    bisaJadwal: false,
    alasanJadwal: "penjadwalannya belum didukung di sini",
  },
  {
    id: "facebook",
    nama: "Facebook",
    batas: 5000,
    cookie: ["c_user", "xs"],
    petunjuk: "Buka facebook.com, salin c_user dan xs ke kotaknya masing-masing.",
    belumTeruji: true,
    bisaJadwal: false,
    alasanJadwal: "penjadwalannya belum didukung di sini",
  },
  {
    id: "threads",
    nama: "Threads",
    batas: 500,
    cookie: ["sessionid"],
    petunjuk: "Buka threads.net, lalu salin nilai cookie sessionid.",
    bisaJadwal: false,
    alasanJadwal: "komposer webnya tidak punya pilihan jadwal",
  },
  {
    id: "twitter",
    nama: "X / Twitter",
    batas: 280,
    cookie: ["auth_token"],
    petunjuk: "Buka x.com, lalu salin nilai cookie auth_token.",
    bisaJadwal: true,
  },
];
