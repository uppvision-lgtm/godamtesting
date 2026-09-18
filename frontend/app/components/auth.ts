/**
 * Sesi login di sisi browser: token disimpan di localStorage, dan setiap
 * permintaan ke backend membawanya lewat header Authorization.
 *
 * Halaman lain cukup memakai `apiFetch` sebagai pengganti `fetch`, dan
 * mendengarkan `PERISTIWA_AKUN` kalau perlu memuat ulang datanya saat orang
 * masuk atau keluar.
 */

import { useSyncExternalStore } from "react";

export const KUNCI_TOKEN = "ig-tools-token";
export const KUNCI_AKUN = "ig-tools-user";
export const KUNCI_PERAN = "ig-tools-peran";
export const PERAN_DEVELOPER = "developer";
/** Dipancarkan di `window` tiap kali ada yang masuk atau keluar. */
export const PERISTIWA_AKUN = "ig-tools-akun";

/**
 * Nama akun yang sedang masuk, atau string kosong kalau belum login.
 *
 * Sengaja tinggal di sini, bukan di formStore: formStore perlu `apiFetch`
 * dari berkas ini, jadi kalau berkas ini juga mengambil sesuatu dari sana,
 * keduanya saling mengimpor.
 */
export function akunSekarang(): string {
  try {
    return window.localStorage.getItem(KUNCI_AKUN) || "";
  } catch {
    return "";
  }
}

/**
 * Peran akun yang sedang masuk. Dipakai halaman hanya untuk memutuskan apakah
 * menu pemantauan perlu ditampilkan; yang menentukan boleh-tidaknya tetap
 * server, yang membaca peran dari database pada setiap permintaan.
 */
export function peranSekarang(): string {
  try {
    return window.localStorage.getItem(KUNCI_PERAN) || "pengguna";
  } catch {
    return "pengguna";
  }
}

export function ambilToken(): string {
  try {
    return window.localStorage.getItem(KUNCI_TOKEN) || "";
  } catch {
    return "";
  }
}

function umumkan() {
  try {
    window.dispatchEvent(new Event(PERISTIWA_AKUN));
  } catch {
    // abaikan
  }
}

export function simpanSesi(token: string, username: string, peran = "pengguna") {
  const sebelum = akunSekarang();
  const peranSebelum = peranSekarang();
  try {
    window.localStorage.setItem(KUNCI_TOKEN, token);
    window.localStorage.setItem(KUNCI_AKUN, username);
    window.localStorage.setItem(KUNCI_PERAN, peran || "pengguna");
  } catch {
    // localStorage bisa diblokir (mode privat) — sesi jadi sebatas halaman ini.
  }
  if (sebelum !== username || peranSebelum !== (peran || "pengguna")) umumkan();
}

export function hapusSesi() {
  const sebelum = akunSekarang();
  try {
    window.localStorage.removeItem(KUNCI_TOKEN);
    window.localStorage.removeItem(KUNCI_AKUN);
    window.localStorage.removeItem(KUNCI_PERAN);
  } catch {
    // abaikan
  }
  if (sebelum) umumkan();
}

/**
 * Id tamu: dipakai orang yang belum masuk supaya tetap bisa membuat dan
 * menyunting template, persis seperti profil tamu di peramban.
 *
 * Disimpan di sessionStorage, bukan localStorage, karena memang tidak boleh
 * tersimpan: begitu tab ditutup, idnya hilang dan server membuang templatenya.
 * Dibuat saat pertama dibutuhkan, jadi sekadar membuka halaman tidak
 * meninggalkan apa pun di server.
 */
const KUNCI_TAMU = "ig-tools-guest";

export function idTamu(): string {
  try {
    const ada = window.sessionStorage.getItem(KUNCI_TAMU);
    if (ada) return ada;
    const acak = new Uint8Array(12);
    window.crypto.getRandomValues(acak);
    const baru = Array.from(acak, (b) => b.toString(16).padStart(2, "0")).join("");
    window.sessionStorage.setItem(KUNCI_TAMU, baru);
    return baru;
  } catch {
    // sessionStorage diblokir: tanpa id tamu, halaman jadi hanya-baca.
    return "";
  }
}

/**
 * `fetch` yang membawa identitas: token login kalau sudah masuk, atau id tamu
 * kalau belum.
 */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = ambilToken();
  const headers = new Headers(init.headers || {});
  if (token) {
    if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  } else if (!headers.has("X-Guest-Id")) {
    const tamu = idTamu();
    if (tamu) headers.set("X-Guest-Id", tamu);
  }
  return fetch(input, { ...init, headers });
}

/**
 * Unggah satu berkas sambil melaporkan kemajuannya.
 *
 * Memakai XMLHttpRequest, bukan fetch: fetch tidak punya cara melaporkan
 * berapa banyak yang sudah terkirim, sedangkan berkas layer bisa puluhan MB
 * dan orang perlu tahu unggahannya benar-benar berjalan.
 *
 * `onProgres` menerima 0-100 untuk bagian pengirimannya saja. Setelah
 * mencapai 100, server masih memeriksa dan merapikan berkasnya, jadi
 * pemanggil sebaiknya menampilkan keadaan "sedang diproses" sampai
 * jawabannya datang.
 */
/** Alamat backend untuk unggahan yang tidak boleh lewat proxy.
 *
 *  Proxy Next di Vercel adalah fungsi serverless dengan batas body beberapa
 *  megabyte saja, jadi video besar tidak akan pernah sampai. Kalau
 *  NEXT_PUBLIC_BACKEND_URL diisi, berkas dikirim langsung ke backend dan
 *  batas itu tidak berlaku. Dibiarkan kosong (mis. di localhost), semuanya
 *  tetap lewat proxy seperti semula.
 */
export const BASIS_BACKEND = (process.env.NEXT_PUBLIC_BACKEND_URL || "").replace(/\/$/, "");

export function apiUnggah(
  url: string,
  file: File,
  onProgres: (persen: number) => void,
  /** Alamat backend untuk dituju LANGSUNG, melewati proxy. Kosong = lewat
   *  proxy seperti biasa. Diisi saat berjalan dari /api/sosmed/info. */
  basis = ""
): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const tujuan = (basis || BASIS_BACKEND).replace(/\/$/, "");
    xhr.open("POST", tujuan ? `${tujuan}${url}` : url);
    const token = ambilToken();
    if (token) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    } else {
      const tamu = idTamu();
      if (tamu) xhr.setRequestHeader("X-Guest-Id", tamu);
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) {
        onProgres(Math.min(100, Math.round((e.loaded / e.total) * 100)));
      }
    };
    xhr.upload.onload = () => onProgres(100);
    xhr.onload = () => {
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(xhr.responseText || "{}");
      } catch {
        data = {};
      }
      // Proxy Next membalas 413 sebagai teks polos, tanpa "detail", sehingga
      // halaman hanya bisa menampilkan pesan umum. Diterjemahkan di sini.
      if (xhr.status === 413 && !data.detail) {
        data = {
          detail:
            "Berkasnya terlalu besar untuk dilewatkan proxy. Muat ulang halaman " +
            "supaya berkas dikirim langsung ke server, lalu coba lagi.",
        };
      }
      resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, data });
    };
    xhr.onerror = () => reject(new Error("Sambungan terputus saat mengunggah."));
    xhr.onabort = () => reject(new Error("Unggahan dibatalkan."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

function langgananAkun(beritahu: () => void): () => void {
  window.addEventListener(PERISTIWA_AKUN, beritahu);
  // Tab lain yang masuk/keluar ikut terlihat di sini.
  window.addEventListener("storage", beritahu);
  return () => {
    window.removeEventListener(PERISTIWA_AKUN, beritahu);
    window.removeEventListener("storage", beritahu);
  };
}

const tanpaLangganan = () => () => {};

/**
 * Nama akun yang sedang masuk, selalu mutakhir. Saat render di server dan
 * saat hidrasi nilainya kosong, lalu diperbarui begitu localStorage terbaca.
 */
export function useAkun(): string {
  return useSyncExternalStore(langgananAkun, akunSekarang, () => "");
}

/** Peran akun yang sedang masuk, selalu mutakhir. Kosong saat render server. */
export function usePeran(): string {
  return useSyncExternalStore(langgananAkun, peranSekarang, () => "pengguna");
}

/** false selama render server dan hidrasi, true setelah halaman hidup di browser. */
export function useSudahHidrasi(): boolean {
  return useSyncExternalStore(tanpaLangganan, () => true, () => false);
}
