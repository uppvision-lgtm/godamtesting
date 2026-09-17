"use client";

/**
 * Pengganti window.prompt dan window.confirm.
 *
 * Dialog bawaan peramban tidak bisa diberi gaya sama sekali, tampil putih
 * menyilaukan di halaman gelap, dan di sebagian peramban malah memberi pilihan
 * "jangan tampilkan lagi" yang membuat tombol jadi diam tanpa penjelasan.
 *
 * Dipakai lewat hook `useDialog()`:
 *
 *   const dialog = useDialog();
 *   const nama = await dialog.tanya({ judul: "Template baru", label: "Nama" });
 *   const ya = await dialog.konfirmasi({ judul: "Hapus?", bahaya: true });
 *   ...
 *   return <>{dialog.simpul}</>;
 */

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./Dialog.module.css";

type Isi = {
  judul: string;
  pesan?: string;
  label?: string;
  nilaiAwal?: string;
  petunjuk?: string;
  tombolYa?: string;
  tombolBatal?: string;
  bahaya?: boolean;
  maxLength?: number;
  /** true = ada kotak isian (pengganti prompt); false = cuma ya/tidak. */
  pakaiIsian: boolean;
};

export function useDialog() {
  const [isi, setIsi] = useState<Isi | null>(null);
  const [nilai, setNilai] = useState("");
  const jawab = useRef<((hasil: string | null) => void) | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const tombolRef = useRef<HTMLButtonElement>(null);

  const tutup = useCallback((hasil: string | null) => {
    const f = jawab.current;
    jawab.current = null;
    setIsi(null);
    setNilai("");
    f?.(hasil);
  }, []);

  useEffect(() => {
    if (!isi) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") tutup(null);
    };
    document.addEventListener("keydown", onKey);
    // Fokus langsung ke tempat yang paling mungkin dipakai.
    const t = window.setTimeout(() => {
      if (isi.pakaiIsian) inputRef.current?.select();
      else tombolRef.current?.focus();
    }, 0);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
    };
  }, [isi, tutup]);

  const buka = useCallback((baru: Isi): Promise<string | null> => {
    // Dialog sebelumnya yang masih menggantung dianggap dibatalkan, supaya
    // pemanggilnya tidak menunggu selamanya.
    jawab.current?.(null);
    setNilai(baru.nilaiAwal ?? "");
    setIsi(baru);
    return new Promise((resolve) => {
      jawab.current = resolve;
    });
  }, []);

  /** Pengganti window.prompt. Mengembalikan teks, atau null kalau dibatalkan. */
  const tanya = useCallback(
    (opsi: Omit<Isi, "pakaiIsian">) => buka({ ...opsi, pakaiIsian: true }),
    [buka]
  );

  /** Pengganti window.confirm. Mengembalikan true kalau disetujui. */
  const konfirmasi = useCallback(
    async (opsi: Omit<Isi, "pakaiIsian" | "label" | "nilaiAwal">) =>
      (await buka({ ...opsi, pakaiIsian: false })) !== null,
    [buka]
  );

  const kirim = () => {
    if (!isi) return;
    if (isi.pakaiIsian) {
      const bersih = nilai.trim();
      if (!bersih) return; // tombol memang dimatikan, ini jaring pengaman
      tutup(bersih);
    } else {
      tutup("ya");
    }
  };

  const simpul = isi ? (
    <div className={styles.latar} onMouseDown={() => tutup(null)}>
      <div
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-judul"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 className={styles.judul} id="dialog-judul">
          {isi.judul}
        </h2>
        {isi.pesan && <p className={styles.pesan}>{isi.pesan}</p>}

        {isi.pakaiIsian && (
          <label className={styles.isian}>
            {isi.label && <span>{isi.label}</span>}
            <input
              ref={inputRef}
              value={nilai}
              maxLength={isi.maxLength ?? 60}
              autoComplete="off"
              onChange={(e) => setNilai(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  kirim();
                }
              }}
            />
          </label>
        )}
        {isi.petunjuk && <p className={styles.petunjuk}>{isi.petunjuk}</p>}

        <div className={styles.aksi}>
          <button type="button" className={styles.batal} onClick={() => tutup(null)}>
            {isi.tombolBatal || "Batal"}
          </button>
          <button
            ref={tombolRef}
            type="button"
            className={isi.bahaya ? styles.bahaya : styles.utama}
            disabled={isi.pakaiIsian && !nilai.trim()}
            onClick={kirim}
          >
            {isi.tombolYa || "Simpan"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return { tanya, konfirmasi, simpul };
}
