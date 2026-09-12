/**
 * Menyimpan isian formulir per akun supaya tidak hilang saat halaman dimuat ulang.
 *
 * Disimpan di localStorage peramban, dan HANYA saat ada akun yang sedang masuk —
 * kalau tidak ada yang login, tidak ada apa pun yang ditulis. Kuncinya memakai
 * nama akun, jadi isian milik dua akun berbeda tidak saling tertukar.
 */

const PREFIX = "ig-tools-form";
export const KUNCI_AKUN = "ig-tools-user";

/** Nama akun yang sedang masuk, atau string kosong kalau belum login. */
export function akunSekarang(): string {
  try {
    return window.localStorage.getItem(KUNCI_AKUN) || "";
  } catch {
    return "";
  }
}

function kunci(halaman: string, akun: string): string {
  return `${PREFIX}:${akun}:${halaman}`;
}

/** Ambil isian tersimpan milik akun yang sedang masuk. */
export function bacaForm<T>(halaman: string, bawaan: T): T {
  const akun = akunSekarang();
  if (!akun) return bawaan;
  try {
    const isi = window.localStorage.getItem(kunci(halaman, akun));
    return isi ? ({ ...bawaan, ...(JSON.parse(isi) as object) } as T) : bawaan;
  } catch {
    return bawaan;
  }
}

/** Simpan isian; diabaikan kalau tidak ada akun yang masuk. */
export function simpanForm(halaman: string, nilai: unknown): void {
  const akun = akunSekarang();
  if (!akun) return;
  try {
    window.localStorage.setItem(kunci(halaman, akun), JSON.stringify(nilai));
  } catch {
    // Kuota penuh atau storage diblokir: isian sekadar tidak tersimpan.
  }
}

/** Hapus seluruh isian tersimpan milik satu akun (dipakai saat keluar). */
export function bersihkanForm(akun: string): void {
  if (!akun) return;
  try {
    const awalan = `${PREFIX}:${akun}:`;
    const dihapus: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(awalan)) dihapus.push(k);
    }
    dihapus.forEach((k) => window.localStorage.removeItem(k));
  } catch {
    // abaikan
  }
}
