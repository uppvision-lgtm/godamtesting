import 'dotenv/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(__dirname, '..');

export const env = (key, fallback = '') => process.env[key] || fallback;

export const PORT = Number(process.env.PORT || 4000);
export const BASE_URL = (process.env.BASE_URL || `http://localhost:${PORT}`).replace(/\/$/, '');

/**
 * Identitas aplikasi untuk halaman legal.
 */
export const APP_NAME = env('APP_NAME', 'Auto Upload Sosmed');
export const APP_CONTACT_EMAIL = env('APP_CONTACT_EMAIL', '');
export const APP_OWNER = env('APP_OWNER', '');

/** Kunci untuk menandatangani cookie sesi. Wajib diganti sebelum dideploy. */
export const APP_SECRET = process.env.APP_SECRET || 'ganti-nilai-ini-sebelum-deploy';

export const PATHS = {
  uploads: path.join(ROOT, 'uploads'),
  data: path.join(ROOT, 'data'),
  db: path.join(ROOT, 'data', 'db.json'),
};

/**
 * Tidak ada lagi kredensial app per platform. Yang disimpan adalah cookie login
 * milik masing-masing pengguna, jadi aplikasi ini tidak butuh daftar app,
 * redirect URI, atau proses review di sisi platform.
 */

/**
 * Rahasia app Meta. Opsional dan hanya dipakai untuk memverifikasi webhook
 * "hapus data" dari Meta — relevan kalau kamu juga punya app Meta sendiri.
 */
export const META_APP_SECRET = env('META_APP_SECRET');

/**
 * Enam platform yang dilayani aplikasi ini.
 * `color` dipakai di UI, `name` untuk label.
 */
export const PLATFORMS = [
  { id: 'instagram', name: 'Instagram Reels', color: '#E1306C' },
  { id: 'tiktok', name: 'TikTok', color: '#FE2C55' },
  { id: 'facebook', name: 'Facebook Page', color: '#1877F2' },
  { id: 'threads', name: 'Threads', color: '#8A8A8A' },
  { id: 'twitter', name: 'X (Twitter)', color: '#CCCCCC' },
  { id: 'youtube', name: 'YouTube Shorts', color: '#FF0000' },
];
export const PLATFORM_IDS = PLATFORMS.map((p) => p.id);
