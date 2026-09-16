import { getConnection } from './db.js';

/**
 * Ambil koneksi milik seorang pengguna untuk satu platform.
 *
 * Cookie sesi tidak punya masa berlaku yang bisa dihitung seperti token OAuth —
 * yang menentukan hidup-matinya adalah apakah pengguna masih login di browser.
 * Jadi di sini koneksi dikembalikan apa adanya; kalau ternyata sudah mati,
 * platform akan menjawab 401/403 dan pesannya muncul di log job.
 *
 * Tombol "Cek ulang" di UI memanggil ulang verify() dan menandai sesi basi.
 */
export async function getFreshConnection(userId, platformId) {
  return getConnection(userId, platformId);
}
