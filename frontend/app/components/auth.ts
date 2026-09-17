/**
 * Sesi login di sisi browser: token disimpan di localStorage, dan setiap
 * permintaan ke backend membawanya lewat header Authorization.
 *
 * Halaman lain cukup memakai `apiFetch` sebagai pengganti `fetch`, dan
 * mendengarkan `PERISTIWA_AKUN` kalau perlu memuat ulang datanya saat orang
 * masuk atau keluar.
 */

import { useSyncExternalStore } from "react";
import { KUNCI_AKUN, akunSekarang } from "./formStore";

export const KUNCI_TOKEN = "ig-tools-token";
/** Dipancarkan di `window` tiap kali ada yang masuk atau keluar. */
export const PERISTIWA_AKUN = "ig-tools-akun";

export { akunSekarang };

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

export function simpanSesi(token: string, username: string) {
  const sebelum = akunSekarang();
  try {
    window.localStorage.setItem(KUNCI_TOKEN, token);
    window.localStorage.setItem(KUNCI_AKUN, username);
  } catch {
    // localStorage bisa diblokir (mode privat) — sesi jadi sebatas halaman ini.
  }
  if (sebelum !== username) umumkan();
}

export function hapusSesi() {
  const sebelum = akunSekarang();
  try {
    window.localStorage.removeItem(KUNCI_TOKEN);
    window.localStorage.removeItem(KUNCI_AKUN);
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

/** false selama render server dan hidrasi, true setelah halaman hidup di browser. */
export function useSudahHidrasi(): boolean {
  return useSyncExternalStore(tanpaLangganan, () => true, () => false);
}
