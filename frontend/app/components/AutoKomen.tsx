"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "../auto-comment/page.module.css";
import { bacaForm, muatFormDariServer, simpanForm } from "./formStore";
import { apiFetch, useAkun } from "./auth";
import type { Akun } from "./sosmed";

type BotResult = {
  comments_posted?: number;
  posts_processed?: number;
};

type LiveStatus = {
  token: string;
  status: string;
  message: string;
  logs: string[];
  result?: BotResult;
};

type CommentTone = "positif" | "netral" | "negatif";

/**
 * Halaman Auto Komen & Like dipakai untuk dua platform. Alur, form, log, dan
 * Chrome Live-nya sama persis; yang berbeda hanya akun mana yang boleh
 * dipilih, nama di judul, dan kunci penyimpanan isiannya - jadi satu
 * komponen dengan satu prop, bukan dua halaman yang disalin.
 */
export type PlatformKomen = "instagram" | "threads";

const INFO_PLATFORM: Record<
  PlatformKomen,
  { nama: string; brand: string; kunci: string; contoh: string }
> = {
  instagram: { nama: "Instagram", brand: "AUTO COMMENT", kunci: "auto-comment", contoh: "tv.mediagaul" },
  threads: {
    nama: "Threads",
    brand: "AUTO COMMENT THREADS",
    kunci: "auto-comment-threads",
    contoh: "topmengudara",
  },
};

type FormValues = {
  // Satu bot = satu akun. Nama dan Session ID-nya diambil server dari akun
  // ini, jadi tidak ada kredensial yang diketik maupun disimpan di peramban.
  account_id: number | null;
  target: string;
  comment_count: number;
  max_posts: number;
  tone: CommentTone;
};

// Satu "slot" bot: punya form, log, dan tampilan Chrome Live sendiri.
type BotSlot = {
  id: number;
  values: FormValues;
  token: string;
  status: string;
  message: string;
  logs: string[];
  result: BotResult | null;
  frameUrl: string;
  error: string;
};

const TONES: { value: CommentTone; icon: string; label: string; hint: string }[] = [
  { value: "positif", icon: "😊", label: "Positif", hint: "Apresiasi & dukung (default)" },
  { value: "netral", icon: "😐", label: "Netral", hint: "Datar, faktual, tanpa memuji" },
  { value: "negatif", icon: "😕", label: "Negatif", hint: "Kritis & sopan, tanpa hinaan" },
];

// Tombol pintas jumlah bot. Angka di luar daftar ini tetap bisa diisi lewat
// kotak angka di sebelahnya — server tidak lagi membatasi jumlah bot.
const PILIHAN_CEPAT = [1, 2, 3, 4, 5];

// Satu-satunya alasan masih ada angka maksimum di sini: setiap bot merender
// satu form panjang beserta panel Chrome Live-nya, jadi angka ekstrem membuat
// browser pengguna sendiri tersendat. Ini batas tampilan, bukan batas server.
const MAX_BOTS = 50;
const BOT_START_GAP_MS = 900;

function initialValues(): FormValues {
  return {
    account_id: null,
    target: "",
    comment_count: 1,
    max_posts: 3,
    tone: "positif",
  };
}

function createBot(id: number): BotSlot {
  return {
    id,
    values: initialValues(),
    token: "",
    status: "",
    message: "",
    logs: [],
    result: null,
    frameUrl: "",
    error: "",
  };
}

function isTerminal(status: string): boolean {
  return status === "completed" || status === "error";
}

// Semua permintaan lewat proxy Next (path relatif), diteruskan ke backend.
function apiUrl(path: string): string {
  return path;
}

export default function AutoKomen({ platform }: { platform: PlatformKomen }) {
  const info = INFO_PLATFORM[platform];
  const [botCount, setBotCount] = useState(1);
  const [bots, setBots] = useState<BotSlot[]>(() => [createBot(1)]);
  const [starting, setStarting] = useState(false);
  // Akun tertaut milik platform ini, untuk pemilih akun tiap bot.
  const [akunIg, setAkunIg] = useState<Akun[]>([]);
  const [perluLogin, setPerluLogin] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const res = await apiFetch("/api/sosmed/accounts", { cache: "no-store" });
        setPerluLogin(res.status === 401);
        if (res.ok) {
          const semua = (await res.json()) as Akun[];
          setAkunIg(semua.filter((a) => a.platform === platform && a.has_session_id));
        }
      } catch {
        // backend mati; pemilih dibiarkan kosong
      }
    })();
  }, [platform]);
  const botsRef = useRef<BotSlot[]>(bots);

  // ===== ISIAN TERSIMPAN PER AKUN =====
  // Dua lapis. Salinan lokal dipasang lebih dulu supaya isian langsung tampil,
  // lalu isian dari server menyusul dan menimpanya — itulah yang membuat
  // isian tetap ada setelah ganti peramban atau pindah laptop. Tamu tidak
  // punya lapisan server, jadi isiannya hilang begitu tab ditutup.
  const akun = useAkun();
  useEffect(() => {
    // Isian dipasang setelah render pertama, bukan lewat nilai awal useState,
    // supaya HTML dari server dan dari browser tetap sama (penyimpanan
    // peramban tidak ada di sisi server).
    const terapkan = (t: { botCount: number; values: FormValues[] } | null) => {
      if (!t?.values?.length) return;
      setBotCount(t.botCount || t.values.length);
      setBots(
        t.values.map((values, i) => ({
          ...createBot(i + 1),
          values: { ...initialValues(), ...values },
        }))
      );
    };
    terapkan(bacaForm<{ botCount: number; values: FormValues[] } | null>(info.kunci, null as never));
    let batal = false;
    void muatFormDariServer<{ botCount: number; values: FormValues[] }>(info.kunci).then(
      (dariServer) => {
        if (!batal) terapkan(dariServer);
      }
    );
    return () => {
      batal = true;
    };
  }, [akun, info.kunci]);

  // Penyimpanan sengaja dipicu dari aksi pengetikan, bukan dari useEffect:
  // efek yang memantau state akan berjalan dengan nilai lama saat halaman baru
  // dimuat dan justru menimpa isian tersimpan dengan nilai kosong.
  const simpanIsian = useCallback((daftar: BotSlot[], jumlah: number) => {
    simpanForm(info.kunci, { botCount: jumlah, values: daftar.map((b) => b.values) });
  }, [info.kunci]);

  const runningBots = bots.filter((bot) => bot.token && !isTerminal(bot.status));

  // Simpan state terbaru di ref supaya polling & penutupan browser saat halaman
  // ditutup tidak perlu memasang ulang interval terus-menerus.
  useEffect(() => {
    botsRef.current = bots;
  }, [bots]);

  // ===== TARIK STATUS SEMUA BOT =====
  const tarikStatus = useCallback(async () => {
    const active = botsRef.current.filter((bot) => bot.token && !isTerminal(bot.status));
    if (active.length === 0) return;
    await Promise.all(
      active.map(async (bot) => {
        try {
          const res = await fetch(apiUrl(`/api/live/${bot.token}/status`), { cache: "no-store" });
          const data: LiveStatus = await res.json();
          setBots((current) =>
            current.map((item) =>
              item.token === bot.token
                ? {
                    ...item,
                    status: data.status,
                    message: data.message || "",
                    logs: Array.isArray(data.logs) ? data.logs : item.logs,
                    result: data.result ?? item.result,
                  }
                : item
            )
          );
        } catch {
          // sementara
        }
      })
    );
  }, []);

  // ===== SEGARKAN TAMPILAN CHROME LIVE TIAP BOT =====
  const segarkanFrame = useCallback(() => {
    setBots((current) =>
      current.map((bot) =>
        bot.token && !isTerminal(bot.status)
          ? { ...bot, frameUrl: `${apiUrl(`/api/live/${bot.token}/frame`)}?t=${Date.now()}` }
          : bot
      )
    );
  }, []);

  // Saat tab disembunyikan (ganti tab/jendela) polling berhenti agar server
  // hemat; begitu kembali dilihat, data langsung ditarik ulang supaya tidak
  // terlihat "beku".
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) return;
      void tarikStatus();
      segarkanFrame();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [tarikStatus, segarkanFrame]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      // Status tetap diambil walau tab disembunyikan (murah & supaya log selalu
      // benar); yang dihemat saat tab tidak dilihat hanya minta frame gambar.
      void tarikStatus();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [tarikStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      segarkanFrame();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [segarkanFrame]);

  // Tutup semua Chromium di backend otomatis saat halaman ditutup/di-refresh,
  // supaya browser tidak menggantung & memori tidak menumpuk.
  useEffect(() => {
    const closeOnUnload = () => {
      botsRef.current.forEach((bot) => {
        if (!bot.token) return;
        try {
          navigator.sendBeacon(apiUrl(`/api/live/${bot.token}/close`), "");
        } catch {
          // abaikan
        }
      });
    };
    window.addEventListener("beforeunload", closeOnUnload);
    return () => {
      window.removeEventListener("beforeunload", closeOnUnload);
    };
  }, []);

  // ===== UBAH JUMLAH BOT (isian bot yang sudah ada tetap dipertahankan) =====
  function changeBotCount(count: number) {
    const next = Math.min(MAX_BOTS, Math.max(1, Number(count) || 1));
    const list = botsRef.current.slice(0, next);
    while (list.length < next) list.push(createBot(list.length + 1));
    const baru = list.map((bot, index) => ({ ...bot, id: index + 1 }));
    setBotCount(next);
    setBots(baru);
    simpanIsian(baru, next);
    setError("");
  }

  // null ikut diterima: account_id kosong saat belum ada akun yang dipilih.
  function updateField(index: number, field: keyof FormValues, value: string | number | null) {
    const baru = botsRef.current.map((bot, i) =>
      i === index ? { ...bot, values: { ...bot.values, [field]: value } } : bot
    );
    setBots(baru);
    simpanIsian(baru, baru.length);
  }

  function validate(values: FormValues): string {
    if (!values.account_id || !values.target.trim()) {
      return "Pilih dulu akunnya, lalu isi channel target.";
    }
    if (!Number.isInteger(values.comment_count) || values.comment_count < 1 || values.comment_count > 100) {
      return "Jumlah komentar harus angka 1-100.";
    }
    if (!Number.isInteger(values.max_posts) || values.max_posts < 1 || values.max_posts > 50) {
      return "Jumlah postingan harus angka 1-50.";
    }
    return "";
  }

  // Satu tombol untuk semua bot. Bot dikirim satu per satu dengan jeda pendek
  // supaya backend tidak membuka banyak Chromium secara serampangan.
  async function startAll() {
    setError("");
    const errors = bots.map((bot) => validate(bot.values));
    if (errors.some((message) => message)) {
      setBots((current) => current.map((bot, i) => ({ ...bot, error: errors[i] })));
      setError("Masih ada form bot yang belum lengkap.");
      return;
    }

    setStarting(true);
    setBots((current) =>
      current.map((bot) => ({
        ...bot,
        error: "",
        token: "",
        status: "starting",
        message: "Menyiapkan browser...",
        logs: [],
        result: null,
        frameUrl: "",
      }))
    );

    for (const bot of bots) {
      try {
        const res = await apiFetch(apiUrl("/api/live/start"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...bot.values, platform }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Gagal memulai.");
        setBots((current) =>
          current.map((item) => (item.id === bot.id ? { ...item, token: data.token } : item))
        );
      } catch (err) {
        const text = err instanceof Error ? err.message : "Gagal memulai.";
        setBots((current) =>
          current.map((item) =>
            item.id === bot.id ? { ...item, error: text, status: "error", message: text } : item
          )
        );
      }
      await new Promise((resolve) => window.setTimeout(resolve, BOT_START_GAP_MS));
    }

    setStarting(false);
  }

  async function stopBot(index: number) {
    const bot = botsRef.current[index];
    if (!bot?.token) return;
    try {
      await fetch(apiUrl(`/api/live/${bot.token}/close`), { method: "POST" });
    } catch {
      // abaikan
    }
    setBots((current) =>
      current.map((item, i) =>
        i === index
          ? {
              ...item,
              token: "",
              status: "",
              message: "",
              logs: [],
              result: null,
              frameUrl: "",
              error: "",
            }
          : item
      )
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.backHome} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>AUTOMASI · {info.brand}</span>
        <span className={styles.headStatus}>
          {runningBots.length > 0 ? `${runningBots.length} bot berjalan` : "idle"}
        </span>
      </header>

      <main className={styles.main}>
        {/* PANEL KONTROL: pilih jumlah bot + satu tombol Mulai untuk semuanya */}
        <section className={styles.controlPanel}>
          <div className={styles.botCountRow}>
            <span className={styles.botCountLabel}>Jumlah bot yang dijalankan</span>
            <div className={styles.botCountOptions}>
              <div className={styles.botCountQuick} role="radiogroup" aria-label="Pilihan cepat jumlah bot">
                {PILIHAN_CEPAT.map((count) => (
                  <button
                    key={count}
                    type="button"
                    role="radio"
                    aria-checked={botCount === count}
                    className={`${styles.botCountBtn} ${
                      botCount === count ? styles.botCountBtnActive : ""
                    }`}
                    onClick={() => changeBotCount(count)}
                  >
                    {count}
                  </button>
                ))}
              </div>
              <div className={styles.botCountStepper}>
                <button
                  type="button"
                  className={styles.botCountBtn}
                  onClick={() => changeBotCount(botCount - 1)}
                  disabled={botCount <= 1}
                  aria-label="Kurangi satu bot"
                >
                  −
                </button>
                <input
                  type="number"
                  className={styles.botCountInput}
                  min={1}
                  max={MAX_BOTS}
                  value={botCount}
                  onChange={(event) => changeBotCount(Number(event.target.value))}
                  aria-label="Jumlah bot"
                />
                <button
                  type="button"
                  className={styles.botCountBtn}
                  onClick={() => changeBotCount(botCount + 1)}
                  disabled={botCount >= MAX_BOTS}
                  aria-label="Tambah satu bot"
                >
                  +
                </button>
              </div>
            </div>
          </div>

          <h1>Jalankan Bot</h1>
          <p className={styles.formSub}>
            Pilih akun yang sudah ditautkan — tanpa CAPTCHA, tanpa menempel apa pun. Satu tombol Mulai untuk {" "}
            {botCount} bot sekaligus.
          </p>
          {botCount > PILIHAN_CEPAT.length && (
            <p className={styles.warnNote}>
              Semua bot berbagi <strong>1 browser</strong> (hemat memori), jadi menjalankan
              banyak bot sekaligus tidak masalah. Yang perlu diingat hanya satu: makin banyak
              bot, makin panjang halaman ini.
            </p>
          )}
          {error && <p className={styles.error} role="alert">{error}</p>}
          <button
            className={styles.startBtn}
            type="button"
            onClick={startAll}
            disabled={starting || runningBots.length > 0}
          >
            {starting ? "Menyiapkan..." : runningBots.length > 0 ? "Berjalan..." : `Mulai ${botCount} bot`}
          </button>
        </section>

        {/* SATU KARTU PER BOT: form + log + tampilan Chrome Live sendiri */}
        <section className={styles.botGrid}>
          {bots.map((bot, index) => (
            <article key={bot.id} className={styles.botCard}>
              <div className={styles.botHeader}>
                <h2>Bot {bot.id}</h2>
                <span className={`${styles.statusBadge} ${bot.status ? styles[bot.status] || "" : ""}`}>
                  {bot.status || "idle"}
                </span>
              </div>

              <div className={styles.panelRow}>
                {/* KIRI: form bot ini */}
                <form className={styles.form} onSubmit={(e) => e.preventDefault()}>
                  <label>
                    <span>Akun</span>
                    <select
                      value={bot.values.account_id ?? ""}
                      onChange={(e) =>
                        updateField(index, "account_id", e.target.value ? Number(e.target.value) : null)
                      }
                    >
                      <option value="">
                        {akunIg.length ? "— pilih akun —" : `belum ada akun ${info.nama} tertaut`}
                      </option>
                      {akunIg.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.nama_akun || a.session_name}
                          {a.verified === false ? " (sesi mati)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Channel target</span>
                    <input
                      value={bot.values.target}
                      onChange={(e) => updateField(index, "target", e.target.value)}
                      placeholder={`mis. ${info.contoh}`}
                      autoComplete="off"
                    />
                  </label>
                  <label>
                    <span>Jumlah komentar / postingan</span>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={bot.values.comment_count}
                      onChange={(e) => updateField(index, "comment_count", Number(e.target.value))}
                    />
                  </label>
                  <label>
                    <span>Postingan terbaru yang dikomentari</span>
                    <input
                      type="number"
                      min={1}
                      max={50}
                      value={bot.values.max_posts}
                      onChange={(e) => updateField(index, "max_posts", Number(e.target.value))}
                    />
                  </label>
                  <fieldset className={styles.toneGroup}>
                    <legend>Jenis komentar</legend>
                    <div className={styles.toneOptions} role="radiogroup" aria-label="Jenis komentar">
                      {TONES.map((option) => (
                        <button
                          key={option.value}
                          type="button"
                          role="radio"
                          aria-checked={bot.values.tone === option.value}
                          title={option.hint}
                          className={`${styles.toneCard} ${
                            bot.values.tone === option.value ? styles.toneCardActive : ""
                          }`}
                          onClick={() => updateField(index, "tone", option.value)}
                        >
                          <span className={styles.toneIcon}>{option.icon}</span>
                          <span className={styles.toneLabel}>{option.label}</span>
                          <span className={styles.toneHint}>{option.hint}</span>
                        </button>
                      ))}
                    </div>
                    {bot.values.tone === "negatif" && (
                      <p className={styles.toneNote}>
                        Komentar negatif tetap sopan: kritik ke isi kontennya, tanpa hinaan, SARA, atau hoaks.
                      </p>
                    )}
                  </fieldset>
                  {bot.error && <p className={styles.error} role="alert">{bot.error}</p>}
                </form>

                {/* KANAN: log bot ini */}
                <aside className={styles.logPanel}>
                  <div className={styles.logHeader}>
                    <h2>Log</h2>
                    <span className={`${styles.statusBadge} ${bot.status ? styles[bot.status] || "" : ""}`}>
                      {bot.status || "idle"}
                    </span>
                  </div>
                  <div className={styles.logBody}>
                    {bot.logs.length === 0 ? (
                      <p className={styles.logEmpty}>Belum ada log.</p>
                    ) : (
                      bot.logs.map((line, i) => (
                        <p key={i} className={styles.logLine}>{line}</p>
                      ))
                    )}
                  </div>
                  {bot.result && (
                    <div className={styles.result}>
                      <strong>{bot.result.comments_posted ?? 0}</strong>
                      <span>komentar</span>
                      <strong>{bot.result.posts_processed ?? 0}</strong>
                      <span>postingan</span>
                    </div>
                  )}
                </aside>
              </div>

              {/* BAWAH: chrome live bot ini */}
              {bot.token && (
                <section className={styles.livePanel}>
                  <div className={styles.liveHeader}>
                    <h2>Chrome Live</h2>
                    <p>{bot.message}</p>
                    {!isTerminal(bot.status) ? (
                      <button className={styles.stopBtn} onClick={() => stopBot(index)}>
                        Stop
                      </button>
                    ) : (
                      <button className={styles.closeBtn} onClick={() => stopBot(index)}>Tutup</button>
                    )}
                  </div>
                  <div className={styles.liveStage}>
                    {bot.frameUrl ? (
                      <img
                        src={bot.frameUrl}
                        alt={`Chrome live bot ${bot.id}`}
                        className={styles.liveFrame}
                        draggable={false}
                      />
                    ) : (
                      <p className={styles.liveHint}>Menunggu frame...</p>
                    )}
                  </div>
                </section>
              )}
            </article>
          ))}
        </section>
      </main>
    </div>
  );
}
