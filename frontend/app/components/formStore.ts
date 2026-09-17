/**
 * Menyimpan isian formulir supaya tidak hilang saat halaman dimuat ulang.
 *
 * Ada dua perilaku, sengaja berbeda:
 *
 * - **Sudah masuk**: isian ikut naik ke server (terenkripsi, lihat
 *   backend/forms_api.py). Tutup tab, ganti peramban, atau pindah laptop —
 *   isiannya tetap ada. localStorage dipakai sebagai salinan cepat supaya
 *   isian langsung tampil tanpa menunggu jaringan.
 * - **Tamu**: hanya sessionStorage. Itu memang maunya — begitu tab ditutup,
 *   semuanya hilang, seperti profil tamu di peramban.
 */

import { akunSekarang, apiFetch } from "./auth";

const PREFIX = "ig-tools-form";

/**
 * Tempat menyimpan salinan lokal. Akun memakai localStorage supaya bertahan;
 * tamu memakai sessionStorage supaya justru TIDAK bertahan.
 */
function gudang(akun: string): Storage | null {
  try {
    return akun ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function kunci(halaman: string, akun: string): string {
  return `${PREFIX}:${akun || "tamu"}:${halaman}`;
}

/** Ambil salinan lokal isian. Cepat, tanpa jaringan. */
export function bacaForm<T>(halaman: string, bawaan: T): T {
  const akun = akunSekarang();
  const tempat = gudang(akun);
  if (!tempat) return bawaan;
  try {
    const isi = tempat.getItem(kunci(halaman, akun));
    return isi ? ({ ...bawaan, ...(JSON.parse(isi) as object) } as T) : bawaan;
  } catch {
    return bawaan;
  }
}

// ===== PENGIRIMAN KE SERVER =====
// Disatukan per halaman dan ditunda sesaat: mengetik satu huruf tidak perlu
// jadi satu permintaan jaringan.
const JEDA_KIRIM_MS = 900;
const tertunda = new Map<string, unknown>();
const pewaktu = new Map<string, number>();

function kirimSekarang(halaman: string, keepalive = false): void {
  const nilai = tertunda.get(halaman);
  if (nilai === undefined) return;
  tertunda.delete(halaman);
  const jam = pewaktu.get(halaman);
  if (jam) {
    window.clearTimeout(jam);
    pewaktu.delete(halaman);
  }
  void apiFetch(`/api/forms/${halaman}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ isi: nilai }),
    // keepalive membuat permintaan tetap terkirim walau tab sedang ditutup.
    // Tidak memakai sendBeacon karena beacon tidak bisa membawa header
    // Authorization, dan tanpa itu server tidak tahu ini isian siapa.
    keepalive,
  }).catch(() => {
    // Gagal menyimpan ke server bukan hal yang perlu mengganggu pengguna:
    // salinan lokalnya sudah tersimpan, dan ketikan berikutnya mencoba lagi.
  });
}

/** Simpan isian: salinan lokal langsung, server menyusul sesaat kemudian. */
export function simpanForm(halaman: string, nilai: unknown): void {
  const akun = akunSekarang();
  const tempat = gudang(akun);
  if (tempat) {
    try {
      tempat.setItem(kunci(halaman, akun), JSON.stringify(nilai));
    } catch {
      // Kuota penuh atau storage diblokir: isian sekadar tidak tersimpan.
    }
  }
  if (!akun) return; // tamu: cukup di peramban, memang tidak ikut ke server
  tertunda.set(halaman, nilai);
  const jam = pewaktu.get(halaman);
  if (jam) window.clearTimeout(jam);
  pewaktu.set(halaman, window.setTimeout(() => kirimSekarang(halaman), JEDA_KIRIM_MS));
}

/** Ambil isian dari server. null = belum pernah menyimpan, atau belum masuk. */
export async function muatFormDariServer<T>(halaman: string): Promise<T | null> {
  if (!akunSekarang()) return null;
  try {
    const res = await apiFetch(`/api/forms/${halaman}`, { cache: "no-store" });
    if (!res.ok) return null; // 401 sesi hangus, 503 belum aktif: pakai lokal
    const data = (await res.json()) as { isi?: T | null };
    if (data.isi === null || data.isi === undefined) return null;
    // Sekalian segarkan salinan lokalnya.
    const tempat = gudang(akunSekarang());
    try {
      tempat?.setItem(kunci(halaman, akunSekarang()), JSON.stringify(data.isi));
    } catch {
      // tidak apa-apa
    }
    return data.isi;
  } catch {
    return null;
  }
}

/** Hapus seluruh isian tersimpan milik satu akun (dipakai saat keluar). */
export function bersihkanForm(akun: string): void {
  if (!akun) return;
  // Yang masih menunggu dikirim dibuang, supaya tidak menyusul setelah keluar.
  tertunda.clear();
  pewaktu.forEach((jam) => window.clearTimeout(jam));
  pewaktu.clear();
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

// Isian yang masih menunggu dikirim diselamatkan saat halaman ditinggalkan.
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    Array.from(tertunda.keys()).forEach((halaman) => kirimSekarang(halaman, true));
  });
}
