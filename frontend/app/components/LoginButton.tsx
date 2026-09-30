"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./LoginButton.module.css";
import { bersihkanForm } from "./formStore";
import {
  PERAN_DEVELOPER,
  PERISTIWA_AKUN,
  akunSekarang,
  ambilToken,
  apiFetch,
  hapusSesi,
  peranSekarang,
  simpanSesi,
  urlApi,
} from "./auth";
import PanelDeveloper from "./PanelDeveloper";

type Mode = "masuk" | "daftar" | "ganti";

const MIN_PASSWORD = 8;

function IkonMata({ tertutup }: { tertutup: boolean }) {
  // Dua ikon garis sederhana: mata terbuka, dan mata dicoret.
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {tertutup && <path d="M3 3l18 18" />}
    </svg>
  );
}

type InputPasswordProps = {
  label: string;
  value: string;
  onChange: (nilai: string) => void;
  autoComplete: "current-password" | "new-password";
  placeholder?: string;
  bantuan?: string;
};

/** Kolom password dengan tombol lihat/sembunyikan di sisi kanannya. */
function InputPassword({ label, value, onChange, autoComplete, placeholder, bantuan }: InputPasswordProps) {
  const [lihat, setLihat] = useState(false);
  return (
    <label>
      <span>{label}</span>
      <span className={styles.pwWrap}>
        <input
          type={lihat ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete={autoComplete}
          placeholder={placeholder}
          spellCheck={false}
        />
        <button
          type="button"
          className={styles.pwToggle}
          onClick={() => setLihat((v) => !v)}
          aria-label={lihat ? "Sembunyikan password" : "Tampilkan password"}
          aria-pressed={lihat}
          tabIndex={-1}
        >
          <IkonMata tertutup={lihat} />
        </button>
      </span>
      {bantuan && <small className={styles.bantu}>{bantuan}</small>}
    </label>
  );
}

const BANTUAN_PASSWORD_BARU = `Minimal ${MIN_PASSWORD} karakter, dicampur angka atau simbol.`;

// Sandi yang paling sering dipakai orang. Daftar yang sama ada di server;
// yang di sini hanya supaya penilaiannya terlihat seketika saat mengetik.
const SANDI_UMUM = new Set([
  "password", "12345678", "123456789", "1234567890", "qwerty123", "qwertyui",
  "password1", "password123", "iloveyou", "admin123", "welcome1", "abc12345",
  "11111111", "00000000", "sayangkamu", "indonesia", "rahasia123",
]);

type Kekuatan = { nilai: 0 | 1 | 2; label: string; kurang: string[] };

/**
 * Nilai kekuatan sandi: 0 lemah (ditolak), 1 sedang, 2 kuat.
 *
 * Aturannya SAMA dengan yang di server (auth_api.kekuatan_password) supaya
 * yang terlihat saat mengetik tidak berbeda dengan yang diterima saat
 * dikirim. Yang menentukan tetap server; ini hanya supaya orang tahu apa
 * yang kurang sebelum menekan tombol.
 */
function nilaiSandi(sandi: string, username: string): Kekuatan {
  const kurang: string[] = [];
  const jenis =
    Number(/[a-z]/.test(sandi)) +
    Number(/[A-Z]/.test(sandi)) +
    Number(/[0-9]/.test(sandi)) +
    Number(/[^a-zA-Z0-9]/.test(sandi));
  if (sandi.length < MIN_PASSWORD) kurang.push(`panjangnya minimal ${MIN_PASSWORD} huruf`);
  if (jenis < 2) kurang.push("campur huruf dengan angka atau simbol");
  if (SANDI_UMUM.has(sandi.toLowerCase())) kurang.push("sandi ini terlalu sering dipakai orang");
  const nama = username.trim().toLowerCase();
  if (nama.length >= 3 && sandi.toLowerCase().includes(nama)) {
    kurang.push("jangan memakai nama akunmu sendiri");
  }
  if (sandi && new Set(sandi).size <= 2) kurang.push("jangan mengulang huruf yang sama");
  const berurutan =
    sandi.length >= 4 &&
    [...sandi].every((c, i) => i === 0 || c.charCodeAt(0) - sandi.charCodeAt(i - 1) === 1);
  if (berurutan) kurang.push("jangan memakai urutan berurutan");

  if (kurang.length) return { nilai: 0, label: "lemah", kurang };
  if (sandi.length >= 12 && jenis >= 3) return { nilai: 2, label: "kuat", kurang };
  return { nilai: 1, label: "sedang", kurang };
}

/** Tiga kotak merah-kuning-hijau plus keterangan apa yang masih kurang. */
function UkuranSandi({ sandi, username }: { sandi: string; username: string }) {
  if (!sandi) return null;
  const hasil = nilaiSandi(sandi, username);
  const warna = ["lemah", "sedang", "kuat"][hasil.nilai];
  return (
    <div className={styles.kuat} aria-live="polite">
      <div className={styles.kuatBar}>
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className={`${styles.kuatKotak} ${i <= hasil.nilai ? styles[`kuat_${warna}`] : ""}`}
          />
        ))}
        <span className={`${styles.kuatLabel} ${styles[`teks_${warna}`]}`}>{hasil.label}</span>
      </div>
      {hasil.kurang.length > 0 && (
        <small className={styles.kuatKurang}>Perlu: {hasil.kurang.join("; ")}.</small>
      )}
    </div>
  );
}

export default function LoginButton() {
  const [terbuka, setTerbuka] = useState(false);
  const [mode, setMode] = useState<Mode>("masuk");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordLama, setPasswordLama] = useState("");
  const [passwordBaru, setPasswordBaru] = useState("");
  const [akun, setAkun] = useState("");
  const [peran, setPeran] = useState("pengguna");
  const [panelDev, setPanelDev] = useState(false);
  const [menuTerbuka, setMenuTerbuka] = useState(false);
  const [galat, setGalat] = useState("");
  const [sukses, setSukses] = useState("");
  const [sibuk, setSibuk] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLSpanElement>(null);

  // Pulihkan sesi yang tersimpan saat halaman dibuka.
  useEffect(() => {
    const token = ambilToken();
    if (!token) return;
    void (async () => {
      try {
        const res = await apiFetch("/api/auth/me", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          setAkun(data.username || "");
          setPeran(data.peran || "pengguna");
          // Pastikan nama akun ikut tersimpan, supaya isian formulir yang
          // dikunci per akun tetap terbaca setelah halaman dimuat ulang.
          simpanSesi(token, data.username || "", data.peran || "pengguna");
        } else if (res.status === 401) {
          // Token hangus: kedaluwarsa, atau password sudah diganti dari
          // perangkat lain.
          hapusSesi();
          setAkun("");
          setPeran("pengguna");
        }
      } catch {
        // backend sedang mati; biarkan tombol Masuk seperti biasa
      }
    })();
  }, []);

  // Komponen lain (mis. halaman edit video) bisa saja mengubah sesi.
  useEffect(() => {
    const sinkron = () => {
      setAkun(akunSekarang());
      setPeran(peranSekarang());
    };
    window.addEventListener(PERISTIWA_AKUN, sinkron);
    return () => window.removeEventListener(PERISTIWA_AKUN, sinkron);
  }, []);

  useEffect(() => {
    if (!terbuka) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setTerbuka(false);
    document.addEventListener("keydown", onKey);
    panelRef.current?.querySelector("input")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [terbuka, mode]);

  // Menu akun menutup saat diklik di luar.
  useEffect(() => {
    if (!menuTerbuka) return;
    const tutup = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuTerbuka(false);
    };
    document.addEventListener("mousedown", tutup);
    return () => document.removeEventListener("mousedown", tutup);
  }, [menuTerbuka]);

  function buka(m: Mode) {
    setMode(m);
    setGalat("");
    setSukses("");
    setPassword("");
    setPasswordLama("");
    setPasswordBaru("");
    setMenuTerbuka(false);
    setTerbuka(true);
  }

  const kirimMasukAtauDaftar = useCallback(async () => {
    setGalat("");
    if (!username.trim() || !password) {
      setGalat("Username dan password wajib diisi.");
      return;
    }
    if (mode === "daftar" && nilaiSandi(password, username).nilai === 0) {
      const kurang = nilaiSandi(password, username).kurang.join("; ");
      setGalat(`Password belum cukup kuat: ${kurang}.`);
      return;
    }
    setSibuk(true);
    try {
      const res = await fetch(urlApi(`/api/auth/${mode === "masuk" ? "login" : "register"}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal. Coba lagi.");
      simpanSesi(data.token, data.username, data.peran || "pengguna");
      setAkun(data.username);
      setPeran(data.peran || "pengguna");
      setTerbuka(false);
      setUsername("");
      setPassword("");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal. Coba lagi.");
    } finally {
      setSibuk(false);
    }
  }, [mode, username, password]);

  const kirimGantiPassword = useCallback(async () => {
    setGalat("");
    setSukses("");
    if (!passwordLama || !passwordBaru) {
      setGalat("Isi password lama dan password baru.");
      return;
    }
    if (nilaiSandi(passwordBaru, username).nilai === 0) {
      setGalat(
        `Password baru belum cukup kuat: ${nilaiSandi(passwordBaru, username).kurang.join("; ")}.`
      );
      return;
    }
    if (passwordBaru === passwordLama) {
      setGalat("Password baru harus berbeda dari password lama.");
      return;
    }
    setSibuk(true);
    try {
      const res = await apiFetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password_lama: passwordLama, password_baru: passwordBaru }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal mengganti password.");
      // Token baru untuk perangkat ini; sesi di perangkat lain sudah keluar.
      simpanSesi(data.token, data.username, data.peran || peran);
      setPasswordLama("");
      setPasswordBaru("");
      setSukses("Password diganti. Perangkat lain yang masih masuk sudah dikeluarkan.");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal mengganti password.");
    } finally {
      setSibuk(false);
    }
    // username ikut: penilaian kekuatan menolak sandi yang memuat nama akun.
  }, [passwordLama, passwordBaru, peran, username]);

  function keluar() {
    // Isian formulir milik akun ini ikut dibersihkan supaya tidak tertinggal
    // di komputer bersama.
    bersihkanForm(akun);
    hapusSesi();
    setAkun("");
    setPeran("pengguna");
    setPanelDev(false);
    setMenuTerbuka(false);
  }

  const judul = mode === "masuk" ? "Masuk" : mode === "daftar" ? "Daftar Akun" : "Ganti Password";

  return (
    <>
      {akun ? (
        <span className={styles.akunWrap} ref={menuRef}>
          <button
            type="button"
            className={styles.akunBtn}
            onClick={() => setMenuTerbuka((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuTerbuka}
          >
            @{akun}
            {peran === PERAN_DEVELOPER && <span className={styles.lencanaDev}>dev</span>}
            <span className={styles.panah} aria-hidden="true">▾</span>
          </button>
          {menuTerbuka && (
            <span className={styles.menu} role="menu">
              {peran === PERAN_DEVELOPER && (
                <button
                  type="button"
                  role="menuitem"
                  className={`${styles.menuItem} ${styles.menuDev}`}
                  onClick={() => {
                    setMenuTerbuka(false);
                    setPanelDev(true);
                  }}
                >
                  Pantau akun
                </button>
              )}
              <button type="button" role="menuitem" className={styles.menuItem} onClick={() => buka("ganti")}>
                Ganti password
              </button>
              <button type="button" role="menuitem" className={`${styles.menuItem} ${styles.menuKeluar}`} onClick={keluar}>
                Keluar
              </button>
            </span>
          )}
        </span>
      ) : (
        <button type="button" className={styles.loginBtn} onClick={() => buka("masuk")}>
          Masuk
        </button>
      )}

      {panelDev && <PanelDeveloper onTutup={() => setPanelDev(false)} />}

      {terbuka && (
        <div className={styles.backdrop} onClick={() => setTerbuka(false)}>
          <div
            ref={panelRef}
            className={styles.panel}
            role="dialog"
            aria-modal="true"
            aria-label={judul}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className={styles.judul}>{judul}</h2>

            {mode === "ganti" ? (
              <form
                className={styles.form}
                onSubmit={(e) => {
                  e.preventDefault();
                  void kirimGantiPassword();
                }}
              >
                {/* Username tersembunyi supaya pengelola password tahu akun mana
                    yang passwordnya berubah. */}
                <input type="text" name="username" value={akun} autoComplete="username" readOnly hidden />
                <InputPassword
                  label="Password lama"
                  value={passwordLama}
                  onChange={setPasswordLama}
                  autoComplete="current-password"
                  placeholder="password yang sekarang"
                />
                <InputPassword
                  label="Password baru"
                  value={passwordBaru}
                  onChange={setPasswordBaru}
                  autoComplete="new-password"
                  placeholder={`minimal ${MIN_PASSWORD} karakter`}
                  bantuan={BANTUAN_PASSWORD_BARU}
                />
                <UkuranSandi sandi={passwordBaru} username={username} />

                {galat && <p className={styles.galat} role="alert">{galat}</p>}
                {sukses && <p className={styles.sukses} role="status">{sukses}</p>}

                <button
                  type="submit"
                  className={styles.kirimBtn}
                  disabled={sibuk || nilaiSandi(passwordBaru, username).nilai === 0}
                >
                  {sibuk ? "Sebentar..." : "Simpan password baru"}
                </button>
              </form>
            ) : (
              <form
                className={styles.form}
                onSubmit={(e) => {
                  e.preventDefault();
                  void kirimMasukAtauDaftar();
                }}
              >
                <label>
                  <span>Username</span>
                  <input
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    placeholder="username kamu"
                  />
                </label>
                <InputPassword
                  label="Password"
                  value={password}
                  onChange={setPassword}
                  autoComplete={mode === "masuk" ? "current-password" : "new-password"}
                  placeholder={mode === "daftar" ? `minimal ${MIN_PASSWORD} karakter` : "password kamu"}
                  bantuan={mode === "daftar" ? BANTUAN_PASSWORD_BARU : undefined}
                />
                {mode === "daftar" && <UkuranSandi sandi={password} username={username} />}

                {galat && <p className={styles.galat} role="alert">{galat}</p>}

                <button
                  type="submit"
                  className={styles.kirimBtn}
                  disabled={
                    sibuk ||
                    (mode === "daftar" && nilaiSandi(password, username).nilai === 0)
                  }
                >
                  {sibuk ? "Sebentar..." : mode === "masuk" ? "Masuk" : "Daftar"}
                </button>
              </form>
            )}

            {mode !== "ganti" && (
              <p className={styles.tukar}>
                {mode === "masuk" ? "Belum punya akun? " : "Sudah punya akun? "}
                <button
                  type="button"
                  className={styles.tautanBiru}
                  onClick={() => {
                    setMode(mode === "masuk" ? "daftar" : "masuk");
                    setGalat("");
                  }}
                >
                  {mode === "masuk" ? "Daftar" : "Masuk"}
                </button>
              </p>
            )}
            {mode === "daftar" && (
              <p className={styles.catatan}>
                Tidak ada email atau nomor telepon yang diminta, jadi tidak ada cara
                memulihkan akun kalau passwordnya lupa. Simpan di pengelola password.
              </p>
            )}
          </div>
        </div>
      )}
    </>
  );
}
