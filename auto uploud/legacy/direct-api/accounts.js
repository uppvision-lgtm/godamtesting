import { getAccount, saveAccount } from './store.js';
import { getPlatform } from './platforms/index.js';

/**
 * Ambil akun dan perbarui token kalau sudah (hampir) kedaluwarsa.
 * Dipanggil sebelum setiap upload.
 */
export async function getFreshAccount(platformId) {
  const account = getAccount(platformId);
  if (!account) return null;

  const platform = getPlatform(platformId);
  const expiringSoon = account.expiresAt && account.expiresAt - Date.now() < 5 * 60 * 1000;
  if (!expiringSoon || typeof platform.refresh !== 'function') return account;

  try {
    const updated = await platform.refresh(account);
    if (updated) return saveAccount(platformId, updated);
  } catch (err) {
    // Biarkan upload mencoba dengan token lama; kalau gagal, pesan error akan jelas.
    console.warn(`[${platformId}] refresh token gagal: ${err.message}`);
  }
  return account;
}
