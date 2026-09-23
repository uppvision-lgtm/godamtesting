"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "../auto-upload/page.module.css";
import lokal from "./page.module.css";
import LoginButton from "../components/LoginButton";
import LogoSosmed, { WARNA } from "../components/LogoSosmed";
import { apiFetch } from "../components/auth";
import { PLATFORM, type Akun } from "../components/sosmed";

/**
 * Auto Edit & Upload: tempel link sumber, videonya diedit dengan template
 * lalu langsung dikirim ke akun-akun sosmed - satu klik.
 *
 * Halaman ini tidak punya mesin sendiri. Ia merangkai dua alat yang sudah
 * ada: Auto Edit Video (/api/video) untuk menyusun videonya, dan Auto Upload
 * (/api/sosmed) untuk mengirimnya. Di antara keduanya ada satu jembatan di
 * server: hasil render disalin menjadi berkas unggahan sosmed.
 *
 * Urutan: baca link -> buat hook -> render per template -> jembatan ->
 * unggah ke akun -> tautan hasil.
 */

type Pratinjau = {
  title: string;
  description: string;
  uploader: string;
  duration: number | null;
  thumbnail: string;
  view_count?: number;
  like_count?: number;
  upload_date?: string;
  extractor?: string;
  width?: number;
  height?: number;
  too_long?: boolean;
  max_seconds?: number;
};

// teks_warna ikut dibaca supaya tombol Hitam/Putih bisa berangkat dari
// setelan template yang sedang dipakai, bukan dari tebakan.
type Template = { id: string; name: string; teks_warna?: string };

type JobStatus = {
  job_id: string;
  template_id: string;
  status: string;
  progress: number;
  output?: string;
  url?: string;
  error?: string;
  message?: string;
};

type Sasaran = {
  id?: number;
  platform: string;
  account_id?: number | null;
  status: string;
  url: string | null;
  error: string | null;
  progress?: number;
  tahap?: string;
};

type Kiriman = {
  template_id: string;
  post_id: number;
  status: string;
  targets: Sasaran[];
};

type Tahap = "idle" | "render" | "jembatan" | "upload" | "selesai" | "gagal";

function selesaiRender(status: string): boolean {
  return status === "done" || status === "error" || status === "dibatalkan";
}

function selesaiKirim(status: string): boolean {
  return status === "done" || status === "failed" || status === "partial";
}

function susunHook(kicker: string, isi: string): string {
  const k = kicker.trim().replace(/!+$/, "").toUpperCase();
  const i = isi.trim().toUpperCase();
  if (!i) return "";
  return k ? `${k}! ${i}` : i;
}

function naskahDariPratinjau(info: Pratinjau | null): string {
  if (!info) return "";
  return [info.title, info.description].filter(Boolean).join(". ");
}

/** 95 detik -> "1:35". */
function formatDurasi(detik: number): string {
  const total = Math.round(detik);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** 218100 -> "218.1 rb". Angka 0 tidak ditulis sama sekali. */
function formatAngka(n: number): string {
  if (!n) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} jt`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} rb`;
  return String(n);
}

/** "20260908" -> "08/09/2026". Bentuk lain dibiarkan kosong. */
function formatTanggal(yyyymmdd: string): string {
  if (!/^\d{8}$/.test(yyyymmdd)) return "";
  return `${yyyymmdd.slice(6, 8)}/${yyyymmdd.slice(4, 6)}/${yyyymmdd.slice(0, 4)}`;
}

const tunda = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default function AutoEditUploadPage() {
  const [link, setLink] = useState("");
  const [pratinjau, setPratinjau] = useState<Pratinjau | null>(null);
  const [membaca, setMembaca] = useState(false);
  const [galatLink, setGalatLink] = useState("");

  const [templates, setTemplates] = useState<Template[]>([]);
  // Tiap akun punya templatenya SENDIRI: sumber videonya sama, tampilannya
  // berbeda per akun. Bukan silang - memilih 3 template tidak berarti satu
  // akun menerima 3 unggahan.
  const [templateAkun, setTemplateAkun] = useState<Record<number, string>>({});

  const [akun, setAkun] = useState<Akun[]>([]);
  const [perluLogin, setPerluLogin] = useState(false);
  const [akunDipilih, setAkunDipilih] = useState<number[]>([]);

  const [caption, setCaption] = useState("");

  // ===== TEKS DI VIDEO =====
  // Sama seperti di Auto Edit Video: saran AI atau ketik sendiri, plus warna
  // tulisannya. Yang dikirim ke perender cuma satu kalimat jadi (teksHook).
  const [modeTeks, setModeTeks] = useState<"ai" | "manual">("ai");
  const [kicker, setKicker] = useState("");
  const [isi, setIsi] = useState("");
  const [kickerManual, setKickerManual] = useState("");
  const [isiManual, setIsiManual] = useState("");
  const [sumberHook, setSumberHook] = useState("");
  const [membuatHook, setMembuatHook] = useState(false);
  // null = ikut setelan tiap template. Halaman ini memakai banyak template
  // sekaligus, jadi memaksa satu warna sebagai bawaan akan merusak template
  // yang sengaja dibuat berwarna lain.
  const [warnaOverride, setWarnaOverride] = useState<"black" | "white" | null>(null);

  const [tahap, setTahap] = useState<Tahap>("idle");
  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [kiriman, setKiriman] = useState<Kiriman[]>([]);
  const [galat, setGalat] = useState("");
  const [catatan, setCatatan] = useState<string[]>([]);

  const berjalan = useRef(false);

  const catat = (baris: string) =>
    setCatatan((lama) => [...lama.slice(-40), `${new Date().toLocaleTimeString("id-ID")}  ${baris}`]);

  // ===== DATA AWAL =====

  const muatAkun = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sosmed/accounts", { cache: "no-store" });
      setPerluLogin(res.status === 401);
      if (res.ok) {
        const daftar = (await res.json()) as Akun[];
        setAkun(daftar);
        // Bawaan: semua akun yang siap tercentang, supaya satu klik memang
        // cukup. Yang sudah terbukti mati tidak ikut.
        setAkunDipilih((lama) =>
          lama.length
            ? lama
            : daftar.filter((a) => a.has_session_id && a.verified !== false).map((a) => a.id)
        );
      }
    } catch {
      // backend mati; dibiarkan
    }
  }, []);

  useEffect(() => {
    void muatAkun();
    void (async () => {
      try {
        const res = await apiFetch("/api/video/templates", { cache: "no-store" });
        if (!res.ok) return;
        const d = (await res.json()) as { templates: Template[] };
        setTemplates(d.templates || []);
      } catch {
        // dibiarkan
      }
    })();
  }, [muatAkun]);

  /** Akun Instagram yang dipakai membaca link sumber dari instagram.com. */
  const akunInstagram = akun.find(
    (a) => a.platform === "instagram" && a.has_session_id && a.verified !== false
  );
  const akunSiap = akun
    .filter((a) => a.has_session_id && a.verified !== false)
    .sort((x, y) => x.platform.localeCompare(y.platform) || x.id - y.id);

  /** Ubah caption jadi satu kalimat hook lewat backend yang sama. */
  const buatHook = useCallback(async (naskah: string) => {
    if (naskah.trim().length < 3) {
      setKicker("");
      setIsi("");
      setSumberHook("Caption masih kosong, jadi teksnya belum bisa dibuat.");
      return;
    }
    setMembuatHook(true);
    try {
      const res = await apiFetch("/api/video/hook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ naskah }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal membuat teks.");
      setKicker(data.kicker || "");
      setIsi(data.body || "");
      setSumberHook(
        data.source === "deepseek"
          ? "Dibuat AI dari caption video."
          : "AI tidak aktif — diringkas langsung dari caption."
      );
    } catch (err) {
      setKicker("");
      setIsi("");
      setSumberHook(err instanceof Error ? err.message : "Gagal membuat teks.");
    } finally {
      setMembuatHook(false);
    }
  }, []);

  // ===== BACA LINK =====

  useEffect(() => {
    const url = link.trim();
    setPratinjau(null);
    setGalatLink("");
    setKicker("");
    setIsi("");
    setSumberHook("");
    if (url.length < 8) return;
    const timer = setTimeout(async () => {
      setMembaca(true);
      try {
        const res = await apiFetch("/api/video/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url,
            // Link Instagram butuh cookie; server mengambilnya dari akun
            // tertaut supaya Session ID tidak perlu diketik di sini.
            account_id: url.includes("instagram.com") ? akunInstagram?.id ?? null : null,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Link tidak bisa dibaca.");
        const info = data as Pratinjau;
        setPratinjau(info);
        setCaption((lama) => lama || info.title || "");
        // Hook dibuat dari judul + deskripsi, sama seperti di Auto Edit Video.
        await buatHook(naskahDariPratinjau(info));
      } catch (err) {
        setGalatLink(err instanceof Error ? err.message : "Link tidak bisa dibaca.");
      } finally {
        setMembaca(false);
      }
    }, 800);
    return () => clearTimeout(timer);
    // akunInstagram sengaja tidak jadi pemicu: berubahnya daftar akun tidak
    // perlu membaca ulang link yang sama.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link]);

  // Kalimat yang benar-benar ditempel ke video: dari AI atau ketikan sendiri.
  const teksHook =
    modeTeks === "ai"
      ? susunHook(kicker, isi)
      : susunHook(kickerManual || kicker || "VIRAL", isiManual);

  // Warna yang ditampilkan: pilihan orangnya kalau ada, kalau tidak ikut
  // template akun pertama supaya pratinjaunya jujur.
  const akunContoh = akunDipilih.find((id) => templateAkun[id]);
  const warnaTemplate = templates.find(
    (x) => x.id === (akunContoh ? templateAkun[akunContoh] : "")
  )?.teks_warna;
  const warnaTampil: "black" | "white" =
    warnaOverride ?? (warnaTemplate === "black" ? "black" : "white");

  const namaSumber = pratinjau?.uploader || "";

  // ===== JALANKAN =====

  const alasanBelumSiap = (() => {
    if (perluLogin) return "Masuk dulu untuk memakai alat ini.";
    if (!link.trim()) return "Tempel dulu link videonya.";
    if (membaca) return "Tunggu sebentar, isi linknya sedang dibaca.";
    if (!pratinjau) return galatLink || "Linknya belum terbaca.";
    if (pratinjau.too_long) {
      return `Videonya lebih dari ${Math.round((pratinjau.max_seconds || 600) / 60)} menit — terlalu panjang untuk diedit.`;
    }
    if (akunDipilih.length === 0) return "Centang dulu minimal satu akun tujuan.";
    const tanpaTemplate = akunDipilih.filter((id) => !templateAkun[id]);
    if (tanpaTemplate.length > 0) {
      const a = akun.find((x) => x.id === tanpaTemplate[0]);
      return `Pilih dulu template untuk ${a?.nama_akun || a?.session_name || "akun itu"}.`;
    }
    if (!caption.trim()) return "Caption wajib diisi.";
    // Hook adalah isi beritanya. Dulu tidak diperiksa: menekan tombol sebelum
    // hook-nya jadi (atau saat kosong) menjalankan render TANPA teks, dan
    // videonya keluar cuma kotak putih - terlihat seperti "isi berita tidak
    // muncul", padahal memang tidak pernah dikirim.
    if (membuatHook) return "Tunggu sebentar, isi beritanya sedang dibuat.";
    if (!teksHook.trim()) return "Isi beritanya masih kosong - tulis sendiri atau buat otomatis dari link.";
    return "";
  })();

  async function jalankan() {
    if (berjalan.current) return;
    berjalan.current = true;
    setGalat("");
    setJobs([]);
    setKiriman([]);
    setCatatan([]);
    setTahap("render");
    try {
      // 1. Render satu video per template yang DIPAKAI. Dua akun yang
      //    memilih template sama cukup dirender sekali.
      const texts: Record<string, string> = {};
      if (teksHook) texts.hook = teksHook;
      if (pratinjau?.uploader) texts.sumber = `SUMBER: ${pratinjau.uploader.toUpperCase()}`;
      const url = link.trim();
      const tujuan = akunDipilih.filter((id) => akunSiap.some((a) => a.id === id));
      const templateUnik = [...new Set(tujuan.map((id) => templateAkun[id]))];
      catat(`Mulai edit: ${templateUnik.length} template untuk ${tujuan.length} akun`);
      const res = await apiFetch("/api/video/jobs/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          template_ids: templateUnik,
          texts,
          // Kosong = tiap template pakai warnanya sendiri.
          teks_warna: warnaOverride || "",
          account_id: url.includes("instagram.com") ? akunInstagram?.id ?? null : null,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Gagal memulai edit.");
      let daftar: JobStatus[] = (data.jobs || []).map(
        (j: { job_id: string; template_id: string; status: string }) => ({
          job_id: j.job_id,
          template_id: j.template_id,
          status: j.status,
          progress: 0,
        })
      );
      setJobs(daftar);

      // 2. Tunggu semua render selesai.
      while (daftar.some((j) => !selesaiRender(j.status))) {
        await tunda(2000);
        daftar = await Promise.all(
          daftar.map(async (j) => {
            if (selesaiRender(j.status)) return j;
            try {
              const r = await apiFetch(`/api/video/jobs/${j.job_id}`, { cache: "no-store" });
              if (!r.ok) return j;
              const s = (await r.json()) as JobStatus;
              return { ...j, ...s, template_id: j.template_id };
            } catch {
              return j;
            }
          })
        );
        setJobs(daftar);
      }
      daftar
        .filter((j) => j.status !== "done")
        .forEach((j) =>
          catat(`Edit gagal (${namaTemplate(j.template_id)}): ${j.error || j.message || j.status}`)
        );
      const jadi = daftar.filter((j) => j.status === "done");
      if (jadi.length === 0) throw new Error("Semua proses edit gagal; tidak ada yang bisa diunggah.");
      catat(`Edit selesai: ${jadi.length} video`);

      // 3. Jembatan: hasil render jadi berkas unggahan, satu per template.
      setTahap("jembatan");
      const mediaTemplate: Record<string, string> = {};
      for (const j of jadi) {
        const r = await apiFetch(`/api/video/jobs/${j.job_id}/ke-sosmed`, { method: "POST" });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          catat(`Gagal menyiapkan ${namaTemplate(j.template_id)}: ${d.detail || r.status}`);
          continue;
        }
        mediaTemplate[j.template_id] = String(d.media_path);
      }

      // 4. Satu kiriman: tiap akun membawa video dari templatenya sendiri.
      setTahap("upload");
      const items = tujuan
        .filter((id) => mediaTemplate[templateAkun[id]])
        .map((id) => ({ account_id: id, media_path: mediaTemplate[templateAkun[id]] }));
      tujuan
        .filter((id) => !mediaTemplate[templateAkun[id]])
        .forEach((id) => {
          const a = akun.find((x) => x.id === id);
          catat(`Dilewati: ${a?.nama_akun || id} - templatenya gagal diedit`);
        });
      if (items.length === 0) throw new Error("Tidak ada video siap untuk diunggah.");

      const ru = await apiFetch("/api/sosmed/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          media_path: items[0].media_path,
          caption: caption.trim(),
          media_kind: "video",
          items,
        }),
      });
      const du = await ru.json().catch(() => ({}));
      if (!ru.ok) throw new Error(du.detail || "Gagal memulai unggahan.");
      const postId = Number(du.post_id);
      catat(`Mengirim ke ${items.length} akun`);
      let semua: Kiriman[] = [{ template_id: "", post_id: postId, status: "processing", targets: [] }];
      setKiriman(semua);

      // 5. Pantau sampai selesai.
      while (semua.some((k) => !selesaiKirim(k.status))) {
        await tunda(3000);
        semua = await Promise.all(
          semua.map(async (k) => {
            if (selesaiKirim(k.status)) return k;
            try {
              const r = await apiFetch(`/api/sosmed/posts/${k.post_id}`, { cache: "no-store" });
              if (!r.ok) return k;
              const p = (await r.json()) as { status: string; targets: Sasaran[] };
              return { ...k, status: p.status, targets: p.targets };
            } catch {
              return k;
            }
          })
        );
        setKiriman(semua);
      }
      const berhasil = semua.flatMap((k) => k.targets).filter((t) => t.status === "success").length;
      const total = semua.flatMap((k) => k.targets).length;
      catat(`Selesai: ${berhasil} dari ${total} unggahan berhasil`);
      setTahap(berhasil > 0 ? "selesai" : "gagal");
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Terjadi galat.");
      setTahap("gagal");
    } finally {
      berjalan.current = false;
    }
  }

  function namaTemplate(id: string): string {
    return templates.find((t) => t.id === id)?.name || id;
  }

  const sedangJalan = tahap === "render" || tahap === "jembatan" || tahap === "upload";
  const persenRender = jobs.length
    ? Math.round(jobs.reduce((j, x) => j + (selesaiRender(x.status) ? 100 : x.progress || 0), 0) / jobs.length)
    : 0;
  const semuaTarget = kiriman.flatMap((k) => k.targets);
  const persenUpload = semuaTarget.length
    ? Math.round(
        semuaTarget.reduce(
          (j, t) => j + (t.status === "success" ? 100 : t.status === "failed" ? 100 : t.progress || 0),
          0
        ) / semuaTarget.length
      )
    : kiriman.length
      ? 0
      : 0;

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">
          ←
        </Link>
        <span className={styles.brand}>AUTOMASI · EDIT &amp; UPLOAD</span>
        <span className={styles.headStatus}>
          {tahap === "idle" ? "siap" : tahap === "selesai" ? "selesai" : tahap === "gagal" ? "gagal" : tahap}
        </span>
        <LoginButton />
      </header>

      <main className={styles.main}>
        {/* ================= MASUKAN ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Satu Klik</h2>
            <span className={styles.headStatus}>edit → upload</span>
          </div>

          {perluLogin && (
            <p className={styles.pesan}>
              Masuk dulu untuk memakai alat ini. Akun tujuannya diambil dari{" "}
              <Link href="/kelola-akun" className={styles.tautan}>
                Kelola Akun
              </Link>
              .
            </p>
          )}

          <div className={styles.form}>
            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Link sumber</h3>
                <span className={styles.jumlahHuruf}>
                  {membaca ? "membaca..." : pratinjau ? "terbaca" : ""}
                </span>
              </div>
              <input
                type="url"
                value={link}
                spellCheck={false}
                placeholder="https://www.instagram.com/reel/... atau link TikTok"
                disabled={sedangJalan}
                onChange={(e) => setLink(e.target.value)}
              />
              {galatLink && <p className={styles.peringatan}>{galatLink}</p>}
              {(membaca || pratinjau) && (
                <div className={lokal.pratinjauBox}>
                  {membaca && (
                    <div className={lokal.pratinjauIsi} aria-label="Sedang membaca isi link">
                      <div className={`${lokal.pratinjauGambar} ${styles.rangka}`} style={{ height: 120 }} />
                      <div className={lokal.pratinjauTeks} style={{ flex: 1 }}>
                        <div className={`${styles.rangka} ${lokal.rangkaBaris}`} style={{ width: "70%" }} />
                        <div className={`${styles.rangka} ${lokal.rangkaBaris}`} style={{ width: "45%" }} />
                      </div>
                    </div>
                  )}
                  {pratinjau && !membaca && (
                    <>
                      <div className={lokal.pratinjauIsi}>
                        {pratinjau.thumbnail && (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            className={lokal.pratinjauGambar}
                            src={pratinjau.thumbnail}
                            alt="Sampul video sumber"
                          />
                        )}
                        <div className={lokal.pratinjauTeks}>
                          <strong>{pratinjau.title || "(tanpa judul)"}</strong>
                          <span>
                            {[
                              pratinjau.uploader,
                              pratinjau.duration ? formatDurasi(pratinjau.duration) : "",
                              pratinjau.width ? `${pratinjau.width}×${pratinjau.height}` : "",
                              pratinjau.view_count ? `${formatAngka(pratinjau.view_count)} tayang` : "",
                              pratinjau.like_count ? `${formatAngka(pratinjau.like_count)} suka` : "",
                              formatTanggal(pratinjau.upload_date || ""),
                              pratinjau.extractor,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </span>
                          {pratinjau.too_long && (
                            <span className={styles.tolak}>
                              Videonya lebih panjang dari batas{" "}
                              {Math.round((pratinjau.max_seconds || 600) / 60)} menit, jadi tidak
                              bisa diedit. Pakai video yang lebih pendek.
                            </span>
                          )}
                        </div>
                      </div>
                      {pratinjau.description && (
                        <p className={lokal.caption}>{pratinjau.description}</p>
                      )}
                    </>
                  )}
                </div>
              )}
              {link.includes("instagram.com") && !akunInstagram && (
                <p className={styles.peringatan}>
                  Link Instagram butuh akun Instagram yang tertaut untuk dibaca. Tautkan dulu di{" "}
                  <Link href="/kelola-akun" className={styles.tautan}>
                    Kelola Akun
                  </Link>
                  .
                </p>
              )}

              {/* ---- Teks yang ditempel ke video, sama isinya dengan
                   Auto Edit Video: saran AI atau ketikan sendiri, warna
                   tulisan, dan kredit di bar bawah. ---- */}
              <div className={lokal.hookBox}>
                <div className={lokal.hookHead}>
                  <span className={lokal.hookJudul}>Teks di video</span>
                  <div className={lokal.pilihMode} title="Warna tulisan isi berita">
                    <button
                      type="button"
                      className={warnaTampil === "black" ? lokal.modeAktif : lokal.modeBtn}
                      disabled={sedangJalan}
                      onClick={() => setWarnaOverride("black")}
                    >
                      Hitam
                    </button>
                    <button
                      type="button"
                      className={warnaTampil === "white" ? lokal.modeAktif : lokal.modeBtn}
                      disabled={sedangJalan}
                      onClick={() => setWarnaOverride("white")}
                    >
                      Putih
                    </button>
                  </div>
                  <div className={lokal.pilihMode} title="Sumber tulisan">
                    <button
                      type="button"
                      className={modeTeks === "ai" ? lokal.modeAktif : lokal.modeBtn}
                      disabled={sedangJalan}
                      onClick={() => setModeTeks("ai")}
                    >
                      AI
                    </button>
                    <button
                      type="button"
                      className={modeTeks === "manual" ? lokal.modeAktif : lokal.modeBtn}
                      disabled={sedangJalan}
                      onClick={() => setModeTeks("manual")}
                    >
                      Manual
                    </button>
                  </div>
                </div>

                {modeTeks === "ai" ? (
                  <>
                    {membuatHook && (
                      <div aria-label="Sedang menyusun tulisan berita">
                        <div className={`${styles.rangka} ${lokal.rangkaBaris}`} style={{ width: "92%" }} />
                        <div className={`${styles.rangka} ${lokal.rangkaBaris}`} style={{ width: "78%" }} />
                      </div>
                    )}
                    {isi && (
                      <p
                        className={`${lokal.hookParagraf} ${
                          warnaTampil === "white" ? lokal.isiPutih : lokal.isiHitam
                        }`}
                      >
                        <span className={lokal.hookKicker}>{kicker || "VIRAL"}!</span> {isi}
                      </p>
                    )}
                    {!membuatHook && !isi && (
                      <p className={styles.hint}>
                        Tempel link video dulu — teksnya disusun dari caption video itu.
                      </p>
                    )}
                    {sumberHook && <p className={styles.hint}>{sumberHook}</p>}
                    {pratinjau && !membuatHook && (
                      <button
                        type="button"
                        className={styles.btnSekunder}
                        disabled={sedangJalan}
                        onClick={() => void buatHook(naskahDariPratinjau(pratinjau))}
                      >
                        Buat ulang
                      </button>
                    )}
                  </>
                ) : (
                  <>
                    <label>
                      <span>Kata pembuka</span>
                      <input
                        value={kickerManual}
                        maxLength={12}
                        disabled={sedangJalan}
                        placeholder={kicker || "VIRAL"}
                        onChange={(e) => setKickerManual(e.target.value)}
                      />
                    </label>
                    <label>
                      <span>Isi berita</span>
                      <textarea
                        className={styles.naskah}
                        rows={3}
                        value={isiManual}
                        maxLength={200}
                        disabled={sedangJalan}
                        placeholder={isi || "maksimal 200 huruf, dipenggal otomatis jadi beberapa baris"}
                        onChange={(e) => setIsiManual(e.target.value)}
                      />
                    </label>
                    {(kickerManual || isiManual) && (
                      <p
                        className={`${lokal.hookParagraf} ${
                          warnaTampil === "white" ? lokal.isiPutih : lokal.isiHitam
                        }`}
                      >
                        <span className={lokal.hookKicker}>
                          {(kickerManual || kicker || "VIRAL").replace(/!+$/, "").toUpperCase()}!
                        </span>{" "}
                        {isiManual.toUpperCase()}
                      </p>
                    )}
                    {isi && (
                      <button
                        type="button"
                        className={styles.btnSekunder}
                        disabled={sedangJalan}
                        onClick={() => {
                          setKickerManual(kicker);
                          setIsiManual(isi);
                        }}
                      >
                        Isi dari saran AI
                      </button>
                    )}
                  </>
                )}

                {warnaOverride && (
                  <p className={styles.hint}>
                    Warna ini dipakai untuk semua template di halaman ini; setelan templatenya
                    sendiri tidak diubah.
                  </p>
                )}
                {namaSumber && (
                  <p className={lokal.kredit}>
                    Kredit di bar bawah: <strong>SUMBER: {namaSumber.toUpperCase()}</strong>
                  </p>
                )}
              </div>
            </div>

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Akun tujuan</h3>
                <div className={styles.aksi}>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    disabled={sedangJalan}
                    onClick={() => {
                      setAkunDipilih(akunSiap.map((a) => a.id));
                      // Yang belum punya template dapat template pertama,
                      // supaya "pilih semua" benar-benar siap dijalankan.
                      if (templates.length) {
                        setTemplateAkun((lama) => {
                          const baru = { ...lama };
                          akunSiap.forEach((a) => {
                            if (!baru[a.id]) baru[a.id] = templates[0].id;
                          });
                          return baru;
                        });
                      }
                    }}
                  >
                    Pilih semua
                  </button>
                  <button
                    type="button"
                    className={styles.btnSekunder}
                    disabled={sedangJalan}
                    onClick={() => setAkunDipilih([])}
                  >
                    Kosongkan
                  </button>
                  <Link href="/kelola-akun" className={styles.btnSekunder}>
                    Kelola akun
                  </Link>
                </div>
              </div>
              {akunSiap.length === 0 ? (
                <p className={styles.hint}>
                  Belum ada akun yang siap. Tempel Session ID-nya di{" "}
                  <Link href="/kelola-akun" className={styles.tautan}>
                    Kelola Akun
                  </Link>
                  .
                </p>
              ) : templates.length === 0 ? (
                <p className={styles.hint}>
                  Belum ada template. Buat dulu di{" "}
                  <Link href="/edit-video" className={styles.tautan}>
                    Auto Edit Video
                  </Link>
                  .
                </p>
              ) : (
                <div className={styles.daftarTujuan}>
                  {akunSiap.map((a) => {
                    const pf = PLATFORM.find((x) => x.id === a.platform);
                    const dicentang = akunDipilih.includes(a.id);
                    return (
                      <div
                        key={a.id}
                        className={`${styles.tujuanBaris} ${dicentang ? styles.tujuanAktif : ""}`}
                      >
                        <label className={styles.tujuanPilih}>
                          <input
                            type="checkbox"
                            checked={dicentang}
                            disabled={sedangJalan}
                            onChange={(e) =>
                              setAkunDipilih((lama) =>
                                e.target.checked ? [...lama, a.id] : lama.filter((x) => x !== a.id)
                              )
                            }
                          />
                          <span className={styles.logoKecil} style={{ color: WARNA[a.platform] }}>
                            <LogoSosmed platform={a.platform} ukuran={15} />
                          </span>
                          <span className={styles.tujuanNama}>
                            {a.nama_akun || a.session_name}
                            {pf?.belumTeruji && (
                              <span className={styles.tandaUji}>belum teruji</span>
                            )}
                          </span>
                        </label>

                        {/* Template khusus akun ini. Sumber videonya sama untuk
                            semua; yang berbeda hanya tampilannya. */}
                        <select
                          className={lokal.pilihTemplate}
                          value={templateAkun[a.id] || ""}
                          disabled={sedangJalan}
                          onChange={(e) =>
                            setTemplateAkun((lama) => ({ ...lama, [a.id]: e.target.value }))
                          }
                        >
                          <option value="">— pilih template —</option>
                          {templates.map((t) => (
                            <option key={t.id} value={t.id}>
                              {t.name}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                </div>
              )}
              <p className={styles.hint}>
                Satu akun = satu template. Sumber videonya sama, tampilannya berbeda per akun.
              </p>
            </div>

            <div className={styles.blok}>
              <div className={styles.blokHead}>
                <h3>Caption</h3>
                <span className={styles.jumlahHuruf}>{caption.length} huruf</span>
              </div>
              <textarea
                rows={4}
                value={caption}
                disabled={sedangJalan}
                placeholder="Terisi otomatis dari judul video; ubah kalau perlu."
                onChange={(e) => setCaption(e.target.value)}
              />
              <p className={styles.hint}>
                Dipakai untuk semua akun. Platform yang batasnya lebih pendek (X 280, Threads 500)
                memotongnya otomatis.
              </p>
            </div>

            <button
              type="button"
              className={styles.btnUtama}
              disabled={!!alasanBelumSiap || sedangJalan}
              onClick={() => void jalankan()}
            >
              {sedangJalan
                ? tahap === "render"
                  ? "Mengedit video..."
                  : tahap === "jembatan"
                    ? "Menyiapkan unggahan..."
                    : "Mengunggah..."
                : `Edit & upload ke ${akunDipilih.length} akun`}
            </button>
            {alasanBelumSiap && !sedangJalan && <p className={styles.alasan}>{alasanBelumSiap}</p>}
            {galat && (
              <p className={styles.galat} role="alert">
                {galat}
              </p>
            )}
          </div>
        </section>

        {/* ================= PROSES ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Proses</h2>
            <span className={styles.headStatus}>
              {tahap === "idle" ? "belum mulai" : tahap}
            </span>
          </div>

          <div className={lokal.langkah}>
            <div className={`${lokal.langkahBaris} ${tahap !== "idle" ? lokal.langkahAktif : ""}`}>
              <span className={lokal.nomor}>1</span>
              <div className={lokal.langkahIsi}>
                <strong>Edit video</strong>
                <div className={styles.hasilProgres}>
                  <span className={styles.progresAngka}>{persenRender}%</span>
                  <span className={styles.progresBatang}>
                    <span className={styles.progresIsi} style={{ width: `${persenRender}%` }} />
                  </span>
                </div>
                {jobs.map((j) => (
                  <div key={j.job_id} className={lokal.subBaris}>
                    <span className={lokal.subNama}>{namaTemplate(j.template_id)}</span>
                    <span className={styles.progresTahap}>
                      {j.status === "done"
                        ? "selesai"
                        : j.status === "error"
                          ? `gagal: ${(j.error || j.message || "").slice(0, 60)}`
                          : `${j.progress || 0}% · ${j.message || j.status}`}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <div
              className={`${lokal.langkahBaris} ${
                tahap === "jembatan" || tahap === "upload" || tahap === "selesai" || tahap === "gagal"
                  ? lokal.langkahAktif
                  : ""
              }`}
            >
              <span className={lokal.nomor}>2</span>
              <div className={lokal.langkahIsi}>
                <strong>Upload ke akun</strong>
                <div className={styles.hasilProgres}>
                  <span className={styles.progresAngka}>{persenUpload}%</span>
                  <span className={styles.progresBatang}>
                    <span className={styles.progresIsi} style={{ width: `${persenUpload}%` }} />
                  </span>
                </div>
                {kiriman.map((k) => (
                  <div key={k.post_id} className={lokal.kirimanBlok}>
                    {k.targets.length === 0 && (
                      <span className={styles.progresTahap}>menyiapkan...</span>
                    )}
                    {k.targets.map((t) => {
                      const a = akun.find((x) => x.id === t.account_id);
                      const nama = a ? a.nama_akun || a.session_name : t.platform;
                      return (
                        <div key={`${k.post_id}-${t.id ?? t.account_id}`} className={lokal.subBaris}>
                          <span className={styles.logoKecil} style={{ color: WARNA[t.platform as keyof typeof WARNA] }}>
                            <LogoSosmed platform={t.platform as keyof typeof WARNA} ukuran={13} />
                          </span>
                          <span className={lokal.subNama}>
                            {nama}
                            {t.account_id && templateAkun[t.account_id]
                              ? ` · ${namaTemplate(templateAkun[t.account_id])}`
                              : ""}
                          </span>
                          {t.status === "success" && t.url ? (
                            <a href={t.url} target="_blank" rel="noreferrer" className={styles.tautan}>
                              Lihat postingan
                            </a>
                          ) : t.status === "failed" ? (
                            <span className={styles.putus}>{(t.error || "gagal").slice(0, 70)}</span>
                          ) : (
                            <span className={styles.progresTahap}>
                              {t.progress || 0}% · {t.tahap || "menunggu giliran"}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
          </div>

          {catatan.length > 0 && (
            <div className={lokal.log}>
              {catatan.map((c, i) => (
                <div key={i}>{c}</div>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
