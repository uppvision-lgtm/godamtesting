"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./page.module.css";
import { bacaForm, simpanForm } from "../components/formStore";

type Overlay = {
  file: string;
  x: number | null;
  y: number | null;
  w?: number | null;
  h?: number | null;
};

type TextLayer = {
  name: string;
  default?: string;
  size?: number;
  color?: string;
  x?: number | null;
  y?: number | null;
  box?: boolean;
  stroke?: number;
};

type Template = {
  id: string;
  name: string;
  width: number;
  height: number;
  fps: number;
  intro: string | null;
  outro: string | null;
  max_duration: number | null;
  overlays: Overlay[];
  texts: TextLayer[];
  assets?: string[];
};

type JobStatus = {
  job_id: string;
  status: string;
  progress: number;
  message?: string;
  logs?: string[];
  output?: string | null;
  error?: string | null;
};

// Batas badan permintaan fungsi serverless Vercel. Aset lebih besar dari ini
// harus diunggah saat frontend berjalan lokal / mode PC.
const BATAS_UNGGAH_MB = 4;

function apiUrl(path: string): string {
  return path;
}

function templateKosong(nama: string): Template {
  return {
    id: "",
    name: nama,
    width: 1080,
    height: 1920,
    fps: 30,
    intro: null,
    outro: null,
    max_duration: null,
    overlays: [],
    texts: [],
    assets: [],
  };
}

const selesai = (status: string) => status === "done" || status === "error";

export default function EditVideoPage() {

  const [templates, setTemplates] = useState<Template[]>([]);
  const [terpilih, setTerpilih] = useState<string>("");
  const [template, setTemplate] = useState<Template | null>(null);
  const [pesanTemplate, setPesanTemplate] = useState("");
  const [menyimpan, setMenyimpan] = useState(false);

  const [link, setLink] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [isiTeks, setIsiTeks] = useState<Record<string, string>>({});
  const [job, setJob] = useState<JobStatus | null>(null);
  const [galat, setGalat] = useState("");
  const [mengirim, setMengirim] = useState(false);
  const jobRef = useRef<string>("");

  // ===== MUAT DAFTAR TEMPLATE =====
  const muatTemplates = useCallback(async (pilih?: string) => {
    try {
      const res = await fetch(apiUrl("/api/video/templates"), { cache: "no-store" });
      const data = await res.json();
      const daftar: Template[] = data.templates || [];
      setTemplates(daftar);
      setTerpilih((sekarang) => pilih || sekarang || daftar[0]?.id || "");
    } catch {
      setGalat("Backend tidak merespons. Pastikan servernya berjalan.");
    }
  }, []);

  useEffect(() => {
    // State baru diubah SETELAH fetch selesai (bukan saat efek berjalan),
    // jadi tidak ada render berantai.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void muatTemplates();
  }, [muatTemplates]);

  // ===== ISIAN TERSIMPAN PER AKUN =====
  useEffect(() => {
    const tersimpan = bacaForm("edit-video", { link: "", sessionId: "", template: "" });
    /* eslint-disable react-hooks/set-state-in-effect -- memulihkan isian
       tersimpan hanya sekali saat halaman dibuka. Sengaja dilakukan setelah
       render pertama, bukan lewat nilai awal useState, supaya HTML dari server
       dan dari browser tetap sama (localStorage tidak ada di server). */
    if (tersimpan.link) setLink(tersimpan.link);
    if (tersimpan.sessionId) setSessionId(tersimpan.sessionId);
    if (tersimpan.template) setTerpilih(tersimpan.template);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  // Sama seperti halaman lain: disimpan saat diubah, bukan lewat useEffect.
  function ubahIsian(bagian: { link?: string; sessionId?: string; template?: string }) {
    const berikutnya = { link, sessionId, template: terpilih, ...bagian };
    if (bagian.link !== undefined) setLink(bagian.link);
    if (bagian.sessionId !== undefined) setSessionId(bagian.sessionId);
    if (bagian.template !== undefined) setTerpilih(bagian.template);
    simpanForm("edit-video", berikutnya);
  }

  // ===== MUAT SATU TEMPLATE (termasuk daftar asetnya) =====
  const muatTemplate = useCallback(async (id: string) => {
    // id kosong berarti sedang menyusun template baru yang belum tersimpan —
    // jangan disentuh, kalau tidak formulirnya ikut hilang saat tekan "+ Baru".
    if (!id) return;
    try {
      const res = await fetch(apiUrl(`/api/video/templates/${id}`), { cache: "no-store" });
      if (!res.ok) return;
      const data: Template = await res.json();
      setTemplate(data);
      const awal: Record<string, string> = {};
      (data.texts || []).forEach((t) => (awal[t.name] = t.default || ""));
      setIsiTeks(awal);
    } catch {
      // biarkan; pesan galat sudah ditangani di tempat lain
    }
  }, []);

  useEffect(() => {
    // Sama seperti di atas: perubahan state terjadi setelah fetch selesai.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void muatTemplate(terpilih);
  }, [terpilih, muatTemplate]);

  // ===== PANTAU JOB =====
  useEffect(() => {
    if (!job?.job_id || selesai(job.status)) return;
    jobRef.current = job.job_id;
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(apiUrl(`/api/video/jobs/${jobRef.current}`), { cache: "no-store" });
        if (!res.ok) return;
        setJob(await res.json());
      } catch {
        // coba lagi pada putaran berikutnya
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.status]);

  function ubahTemplate(bagian: Partial<Template>) {
    setTemplate((current) => (current ? { ...current, ...bagian } : current));
  }

  async function simpanTemplate() {
    if (!template) return;
    setMenyimpan(true);
    setPesanTemplate("");
    try {
      const id = template.id || template.name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      const res = await fetch(apiUrl(`/api/video/templates?template_id=${encodeURIComponent(id)}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: template.name,
          width: template.width,
          height: template.height,
          fps: template.fps,
          intro: template.intro || null,
          outro: template.outro || null,
          max_duration: template.max_duration || null,
          overlays: template.overlays,
          texts: template.texts,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal menyimpan template.");
      setPesanTemplate("Template tersimpan.");
      await muatTemplates(data.id);
      await muatTemplate(data.id);
    } catch (err) {
      setPesanTemplate(err instanceof Error ? err.message : "Gagal menyimpan template.");
    } finally {
      setMenyimpan(false);
    }
  }

  async function unggahAset(file: File) {
    if (!template?.id) {
      setPesanTemplate("Simpan template dulu sebelum mengunggah aset.");
      return;
    }
    if (file.size > BATAS_UNGGAH_MB * 1_048_576) {
      setPesanTemplate(
        `Berkas ${(file.size / 1_048_576).toFixed(1)} MB terlalu besar lewat website ` +
          `(batas ${BATAS_UNGGAH_MB} MB).`
      );
      return;
    }
    setPesanTemplate(`Mengunggah ${file.name} ...`);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(apiUrl(`/api/video/templates/${template.id}/assets`), {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal mengunggah.");
      setPesanTemplate(`${data.file} terunggah.`);
      await muatTemplate(template.id);
    } catch (err) {
      setPesanTemplate(err instanceof Error ? err.message : "Gagal mengunggah.");
    }
  }

  async function mulaiRender() {
    setGalat("");
    if (!terpilih) return setGalat("Pilih template dulu.");
    if (!link.trim()) return setGalat("Tempel link videonya dulu.");
    setMengirim(true);
    try {
      const res = await fetch(apiUrl("/api/video/jobs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: link.trim(),
          template_id: terpilih,
          texts: isiTeks,
          session_id: sessionId.trim(),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Gagal memulai.");
      setJob({ job_id: data.job_id, status: "queued", progress: 0, logs: [] });
    } catch (err) {
      setGalat(err instanceof Error ? err.message : "Gagal memulai.");
    } finally {
      setMengirim(false);
    }
  }

  const berkasVideo = job?.output ? apiUrl(`/api/video/jobs/${job.job_id}/file`) : "";
  const aset = template?.assets || [];
  const asetVideo = aset.filter((a) => /\.(mp4|mov|m4v|webm)$/i.test(a));
  const asetGambar = aset.filter((a) => /\.(png|jpe?g|webp)$/i.test(a));

  return (
    <div className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.back} title="Kembali ke beranda">←</Link>
        <span className={styles.brand}>IG · EDIT VIDEO</span>
        <span className={styles.headStatus}>{job ? job.status : "idle"}</span>
      </header>

      <main className={styles.main}>
        {/* ================= TEMPLATE ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Template Layer</h2>
            <div className={styles.headActions}>
              <select
                className={styles.select}
                value={terpilih}
                onChange={(e) => ubahIsian({ template: e.target.value })}
              >
                {templates.length === 0 && <option value="">(belum ada template)</option>}
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={() => {
                  setTemplate(templateKosong("Template Baru"));
                  setTerpilih("");
                }}
              >
                + Baru
              </button>
            </div>
          </div>

          {!template ? (
            <p className={styles.hint}>Pilih template di atas, atau buat baru.</p>
          ) : (
            <div className={styles.form}>
              <div className={styles.row}>
                <label>
                  <span>Nama template</span>
                  <input
                    value={template.name}
                    onChange={(e) => ubahTemplate({ name: e.target.value })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>Lebar</span>
                  <input
                    type="number"
                    value={template.width}
                    onChange={(e) => ubahTemplate({ width: Number(e.target.value) })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>Tinggi</span>
                  <input
                    type="number"
                    value={template.height}
                    onChange={(e) => ubahTemplate({ height: Number(e.target.value) })}
                  />
                </label>
                <label className={styles.kecil}>
                  <span>FPS</span>
                  <input
                    type="number"
                    value={template.fps}
                    onChange={(e) => ubahTemplate({ fps: Number(e.target.value) })}
                  />
                </label>
              </div>

              {/* ---- ASET ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Aset layer</h3>
                  <label className={styles.uploadBtn}>
                    + Unggah berkas
                    <input
                      type="file"
                      accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.m4v,.webm"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) void unggahAset(f);
                        e.target.value = "";
                      }}
                    />
                  </label>
                </div>
                {aset.length === 0 ? (
                  <p className={styles.hint}>
                    Belum ada aset. Unggah PNG untuk overlay, dan MP4 untuk intro/outro.
                  </p>
                ) : (
                  <ul className={styles.asetList}>
                    {aset.map((a) => (
                      <li key={a}><code>{a}</code></li>
                    ))}
                  </ul>
                )}
              </div>

              {/* ---- INTRO / OUTRO ---- */}
              <div className={styles.row}>
                <label>
                  <span>Intro</span>
                  <select
                    value={template.intro || ""}
                    onChange={(e) => ubahTemplate({ intro: e.target.value || null })}
                  >
                    <option value="">(tanpa intro)</option>
                    {asetVideo.map((a) => (
                      <option key={a} value={`assets/${a}`}>{a}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Outro</span>
                  <select
                    value={template.outro || ""}
                    onChange={(e) => ubahTemplate({ outro: e.target.value || null })}
                  >
                    <option value="">(tanpa outro)</option>
                    {asetVideo.map((a) => (
                      <option key={a} value={`assets/${a}`}>{a}</option>
                    ))}
                  </select>
                </label>
                <label className={styles.kecil}>
                  <span>Durasi maks (detik)</span>
                  <input
                    type="number"
                    value={template.max_duration ?? ""}
                    placeholder="penuh"
                    onChange={(e) =>
                      ubahTemplate({ max_duration: e.target.value ? Number(e.target.value) : null })
                    }
                  />
                </label>
              </div>

              {/* ---- OVERLAY ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Overlay gambar</h3>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    disabled={asetGambar.length === 0}
                    onClick={() =>
                      ubahTemplate({
                        overlays: [
                          ...template.overlays,
                          { file: `assets/${asetGambar[0]}`, x: 0, y: 0 },
                        ],
                      })
                    }
                  >
                    + Tambah
                  </button>
                </div>
                {template.overlays.map((ov, i) => (
                  <div key={i} className={styles.row}>
                    <label>
                      <span>Berkas</span>
                      <select
                        value={ov.file}
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, file: e.target.value };
                          ubahTemplate({ overlays: baru });
                        }}
                      >
                        {asetGambar.map((a) => (
                          <option key={a} value={`assets/${a}`}>{a}</option>
                        ))}
                      </select>
                    </label>
                    <label className={styles.kecil}>
                      <span>X</span>
                      <input
                        type="number"
                        value={ov.x ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, x: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ overlays: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Y</span>
                      <input
                        type="number"
                        value={ov.y ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.overlays];
                          baru[i] = { ...ov, y: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ overlays: baru });
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      className={styles.hapusBtn}
                      onClick={() =>
                        ubahTemplate({ overlays: template.overlays.filter((_, j) => j !== i) })
                      }
                    >
                      Hapus
                    </button>
                  </div>
                ))}
              </div>

              {/* ---- TEKS ---- */}
              <div className={styles.blok}>
                <div className={styles.blokHead}>
                  <h3>Teks dinamis</h3>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={() =>
                      ubahTemplate({
                        texts: [
                          ...template.texts,
                          { name: `teks${template.texts.length + 1}`, size: 64, y: 150, color: "white" },
                        ],
                      })
                    }
                  >
                    + Tambah
                  </button>
                </div>
                {template.texts.map((t, i) => (
                  <div key={i} className={styles.row}>
                    <label>
                      <span>Nama isian</span>
                      <input
                        value={t.name}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, name: e.target.value };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Ukuran</span>
                      <input
                        type="number"
                        value={t.size ?? 64}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, size: Number(e.target.value) };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Y</span>
                      <input
                        type="number"
                        value={t.y ?? ""}
                        placeholder="tengah"
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, y: e.target.value === "" ? null : Number(e.target.value) };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <label className={styles.kecil}>
                      <span>Warna</span>
                      <input
                        value={t.color ?? "white"}
                        onChange={(e) => {
                          const baru = [...template.texts];
                          baru[i] = { ...t, color: e.target.value };
                          ubahTemplate({ texts: baru });
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      className={styles.hapusBtn}
                      onClick={() => ubahTemplate({ texts: template.texts.filter((_, j) => j !== i) })}
                    >
                      Hapus
                    </button>
                  </div>
                ))}
              </div>

              <div className={styles.aksi}>
                <button className={styles.simpanBtn} onClick={simpanTemplate} disabled={menyimpan}>
                  {menyimpan ? "Menyimpan..." : "Simpan template"}
                </button>
                {pesanTemplate && <span className={styles.pesan}>{pesanTemplate}</span>}
              </div>
            </div>
          )}
        </section>

        {/* ================= RENDER ================= */}
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Buat Video</h2>
          </div>
          <div className={styles.form}>
            <label>
              <span>Link video sumber</span>
              <input
                value={link}
                onChange={(e) => ubahIsian({ link: e.target.value })}
                placeholder="https://www.instagram.com/reel/... atau TikTok / YouTube / link MP4"
              />
            </label>
            {link.toLowerCase().includes("instagram.com") && (
              <label>
                <span>Session ID (untuk link Instagram)</span>
                <input
                  value={sessionId}
                  onChange={(e) => ubahIsian({ sessionId: e.target.value })}
                  placeholder="tempel sessionid cookie"
                  autoComplete="off"
                />
              </label>
            )}

            {(template?.texts || []).map((t) => (
              <label key={t.name}>
                <span>Teks: {t.name}</span>
                <input
                  value={isiTeks[t.name] ?? ""}
                  onChange={(e) => setIsiTeks({ ...isiTeks, [t.name]: e.target.value })}
                  placeholder={t.default || "(kosongkan untuk melewati)"}
                />
              </label>
            ))}

            {galat && <p className={styles.error} role="alert">{galat}</p>}

            <button
              className={styles.mulaiBtn}
              onClick={mulaiRender}
              disabled={mengirim || (!!job && !selesai(job.status))}
            >
              {job && !selesai(job.status) ? "Sedang dibuat..." : "Buat video"}
            </button>

            {job && (
              <div className={styles.jobBox}>
                <div className={styles.jobHead}>
                  <span className={`${styles.badge} ${styles[job.status] || ""}`}>{job.status}</span>
                  <span>{job.message}</span>
                </div>
                <div className={styles.barWrap}>
                  <div className={styles.bar} style={{ width: `${job.progress || 0}%` }} />
                </div>
                <div className={styles.logBody}>
                  {(job.logs || []).map((baris, i) => (
                    <p key={i}>{baris}</p>
                  ))}
                </div>
                {job.status === "done" && berkasVideo && (
                  <div className={styles.hasil}>
                    <video className={styles.video} src={berkasVideo} controls playsInline />
                    <a className={styles.unduhBtn} href={berkasVideo} download>
                      Unduh MP4
                    </a>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
