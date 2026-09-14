"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./LoginButton.module.css";
import { KUNCI_AKUN, bersihkanForm } from "./formStore";

type Mode = "masuk" | "daftar";

const KUNCI_TOKEN = "ig-tools-token";

function apiUrl(path: string): string {
  return path;
}

function simpanSesi(token: string, username: string) {
  try {
    window.localStorage.setItem(KUNCI_TOKEN, token);
    window.localStorage.setItem(KUNCI_AKUN, username);
  } catch {
    // localStorage bisa diblokir (mode privat) — sesi jadi sebatas halaman ini.
  }
}

function hapusSesi() {
  try {
    window.localStorage.removeItem(KUNCI_TOKEN);
    window.localStorage.removeItem(KUNCI_AKUN);
  } catch {
    // abaikan
  }
}

function ambilToken(): string {
  try {
    return window.localStorage.getItem(KUNCI_TOKEN) || "";
  } catch {
    return "";
  }
}

export default function LoginButton() {
  const [terbuka, setTerbuka] = useState(false);
  const [mode, setMode] = useState<Mode>("masuk");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [akun, setAkun] = useState("");
  const [galat, setGalat] = useState("");
  const [sibuk, setSibuk] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Pulihkan sesi yang tersimpan saat halaman dibuka.
  useEffect(() => {
    const token = ambilToken();
    if (!token) return;
    void (async () => {
      try {
        const res = await fetch(apiUrl("/api/auth/me"), {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (res.ok) {
          const data = await res.json();
          setAkun(data.username || "");
          // Pastikan nama akun ikut tersimpan, supaya isian formulir yang
          // dikunci per akun tetap terbaca setelah halaman dimuat ulang.
          simpanSesi(token, data.username || "");
        } else {
          hapusSesi();
        }
      } catch {
        // backend sedang mati; biarkan tombol Masuk seperti biasa
      }
    })();
  }, []);

  useEffect(() => {
    if (!terbuka) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setTerbuka(false);
    document.addEventListener("keydown", onKey);
    panelRef.current?.querySelector("input")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [terbuka, mode]);

  const kirim = useCallback(async () => {
    setGalat("");
    if (!username.trim() || !password) {
      setGalat("Username dan password wajib diisi.");
      return;
    }
    if (mode === "daftar" && password.length < 8) {
      setGalat("Password minimal 8 karakter.");
      return;
    }
    setSibuk(true);
    try {
      const res = await fetch(apiUrl(`/api/auth/${mode === "masuk" ? "login" : "register"}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal. Coba lagi.");
      simpanSesi(data.token, data.username);
      setAkun(data.username);
      setTerbuka(false);
      setUsername("");
      setPassword("");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal. Coba lagi.");
    } finally {
      setSibuk(false);
    }
  }, [mode, username, password]);

  function keluar() {
    // Isian formulir milik akun ini ikut dibersihkan supaya tidak tertinggal
    // di komputer bersama.
    bersihkanForm(akun);
    hapusSesi();
    setAkun("");
  }

  if (akun) {
    return (
      <span className={styles.akunWrap}>
        <span className={styles.akun}>@{akun}</span>
        <button type="button" className={styles.keluarBtn} onClick={keluar}>
          Keluar
        </button>
      </span>
    );
  }

  return (
    <>
      <button type="button" className={styles.loginBtn} onClick={() => setTerbuka(true)}>
        Masuk
      </button>

      {terbuka && (
        <div className={styles.backdrop} onClick={() => setTerbuka(false)}>
          <div
            ref={panelRef}
            className={styles.panel}
            role="dialog"
            aria-modal="true"
            aria-label={mode === "masuk" ? "Masuk" : "Daftar akun"}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className={styles.judul}>{mode === "masuk" ? "Masuk" : "Daftar Akun"}</h2>

            <form
              className={styles.form}
              onSubmit={(e) => {
                e.preventDefault();
                void kirim();
              }}
            >
              <label>
                <span>Username</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  placeholder="username kamu"
                />
              </label>
              <label>
                <span>Password</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={mode === "masuk" ? "current-password" : "new-password"}
                  placeholder={mode === "daftar" ? "minimal 8 karakter" : "password kamu"}
                />
              </label>

              {galat && <p className={styles.galat} role="alert">{galat}</p>}

              <button type="submit" className={styles.kirimBtn} disabled={sibuk}>
                {sibuk ? "Sebentar..." : mode === "masuk" ? "Masuk" : "Daftar"}
              </button>
            </form>

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
          </div>
        </div>
      )}
    </>
  );
}
