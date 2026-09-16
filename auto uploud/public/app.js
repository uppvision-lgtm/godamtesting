const $ = (id) => document.getElementById(id);
const api = async (url, opts = {}) => {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Gagal (${res.status})`);
  return body;
};

let me = null;
let platforms = [];
let selected = new Set();
let videoFile = null;

/* ───────────── Login / daftar ───────────── */

let authMode = 'login';

document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    authMode = tab.dataset.mode;
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t === tab));
    $('authName').hidden = authMode === 'login';
    $('authSubmit').textContent = authMode === 'login' ? 'Masuk' : 'Daftar';
    $('authPassword').autocomplete = authMode === 'login' ? 'current-password' : 'new-password';
    $('authError').hidden = true;
  };
});

$('authForm').onsubmit = async (e) => {
  e.preventDefault();
  $('authError').hidden = true;
  try {
    const { user } = await api(`/api/auth/${authMode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('authName').value,
        email: $('authEmail').value,
        password: $('authPassword').value,
      }),
    });
    me = user;
    showApp();
  } catch (err) {
    $('authError').textContent = err.message;
    $('authError').hidden = false;
  }
};

$('deleteAccount').onclick = async () => {
  if (!confirm('Hapus akunmu beserta SEMUA token sosial media? Tindakan ini permanen.')) return;
  if (!confirm('Sekali lagi: ini tidak bisa dibatalkan. Lanjutkan?')) return;
  await api('/api/auth/me', { method: 'DELETE' });
  alert('Akun dan seluruh datamu sudah dihapus.');
  location.reload();
};

$('logout').onclick = async () => {
  await api('/api/auth/logout', { method: 'POST' });
  location.reload();
};

async function boot() {
  const { user, firstUser } = await api('/api/auth/me');
  me = user;
  if (me) return showApp();

  $('authView').hidden = false;
  $('authHint').textContent = firstUser
    ? 'Belum ada pengguna. Akun pertama yang didaftarkan otomatis jadi admin.'
    : 'Tiap pengguna punya akun sosialnya sendiri dan tidak bisa melihat milik orang lain.';
}

function showApp() {
  $('authView').hidden = true;
  $('appView').hidden = false;
  $('userBox').hidden = false;
  $('userName').textContent = `${me.name}${me.role === 'admin' ? ' · admin' : ''}`;
  $('deleteAccount').hidden = false;
  loadSocial();
}

/* ───────────── Akun sosial (mode sesi/cookie) ───────────── */

async function loadSocial() {
  $('socialGrid').innerHTML = '<p class="hint">Memuat status akun…</p>';
  try {
    const data = await api('/api/social/status');
    platforms = data.platforms;

    $('socialGrid').innerHTML = '';
    for (const p of platforms) $('socialGrid').appendChild(socialCard(p));
  } catch (err) {
    $('socialGrid').innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
  renderTargets();
  updateSendState();
}

function socialCard(p) {
  const el = document.createElement('div');
  el.className = `social ${p.connected ? 'on' : ''}`;

  const state = !p.connected
    ? 'Belum tertaut'
    : p.stale
      ? `${p.displayName || 'Tersambung'} · sesi perlu dicek`
      : p.displayName || 'Tersambung';

  el.innerHTML = `
    <span class="dot" style="background:${p.connected ? p.color : 'var(--line)'}"></span>
    <div class="social-info"><b>${p.name}</b><span>${esc(state)}</span></div>`;

  if (p.connected) {
    const check = document.createElement('button');
    check.className = 'small ghost';
    check.textContent = 'Cek ulang';
    check.onclick = async () => {
      check.disabled = true;
      check.textContent = 'Mengecek…';
      try {
        await api(`/api/social/${p.id}/check`, { method: 'POST' });
        alert(`${p.name}: sesi masih aktif.`);
      } catch (err) {
        alert(`${p.name}: ${err.message}`);
      }
      loadSocial();
    };
    el.appendChild(check);
  }

  const btn = document.createElement('button');
  btn.className = 'small ' + (p.connected ? 'ghost danger' : '');
  btn.textContent = p.connected ? 'Putuskan' : 'Hubungkan';
  btn.onclick = p.connected
    ? async () => {
        if (!confirm(`Putuskan ${p.name}? Cookie sesi untuk platform ini akan dihapus.`)) return;
        await api(`/api/social/${p.id}/disconnect`, { method: 'POST' });
        loadSocial();
      }
    : () => openConnect(p);
  el.appendChild(btn);
  return el;
}

$('refreshSocial').onclick = loadSocial;

/* ───────────── Jendela tempel cookie ───────────── */

let connecting = null;

function openConnect(platform) {
  connecting = platform;

  $('connectTitle').textContent = `Hubungkan ${platform.name}`;
  $('connectIntro').textContent = platform.intro || '';
  $('connectSteps').innerHTML = (platform.steps || []).map((s) => `<li>${esc(s)}</li>`).join('');
  $('connectFields').innerHTML = platform.fields
    .map(
      (f) => `
      <label class="field">
        <span>${esc(f.label)}${f.required ? '' : ' <i class="muted">opsional</i>'}</span>
        <input type="password" data-key="${esc(f.key)}" autocomplete="off" spellcheck="false"
               placeholder="tempel nilainya di sini" />
      </label>`,
    )
    .join('');

  $('connectShow').checked = false;
  $('connectError').hidden = true;
  $('connectSubmit').disabled = false;
  $('connectSubmit').textContent = 'Simpan & cek';
  $('connectModal').hidden = false;
  $('connectFields').querySelector('input')?.focus();
}

function closeConnect() {
  $('connectModal').hidden = true;
  connecting = null;
}

$('connectClose').onclick = closeConnect;
$('connectModal').onclick = (e) => {
  if (e.target === $('connectModal')) closeConnect();
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('connectModal').hidden) closeConnect();
});
$('connectShow').onchange = (e) => {
  const type = e.target.checked ? 'text' : 'password';
  $('connectFields').querySelectorAll('input').forEach((i) => (i.type = type));
};

$('connectForm').onsubmit = async (e) => {
  e.preventDefault();
  if (!connecting) return;

  const fields = {};
  $('connectFields').querySelectorAll('input').forEach((i) => (fields[i.dataset.key] = i.value.trim()));

  const platformName = connecting.name;
  $('connectSubmit').disabled = true;
  $('connectSubmit').textContent = 'Memeriksa ke platform…';
  $('connectError').hidden = true;

  try {
    const res = await api(`/api/social/${connecting.id}/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields }),
    });
    closeConnect();
    alert(`✅ ${platformName} tersambung${res.displayName ? ` sebagai ${res.displayName}` : ''}.`);
    loadSocial();
  } catch (err) {
    $('connectError').textContent = err.message;
    $('connectError').hidden = false;
    $('connectSubmit').disabled = false;
    $('connectSubmit').textContent = 'Simpan & cek';
  }
};

/* ───────────── Pilih platform tujuan ───────────── */

function renderTargets() {
  $('targets').innerHTML = '';
  for (const p of platforms) {
    const usable = p.connected;
    if (!usable) selected.delete(p.id);

    const el = document.createElement('label');
    el.className = `chip ${usable ? (selected.has(p.id) ? 'on' : '') : 'off'}`;
    el.title = usable ? '' : 'Hubungkan akun ini dulu';
    el.innerHTML = `<input type="checkbox" ${selected.has(p.id) ? 'checked' : ''} ${usable ? '' : 'disabled'}>
      <span class="dot" style="background:${p.color}"></span>${p.name}`;
    el.querySelector('input').onchange = (ev) => {
      ev.target.checked ? selected.add(p.id) : selected.delete(p.id);
      renderTargets();
      updateSendState();
    };
    $('targets').appendChild(el);
  }
}

/* ───────────── File video ───────────── */

const drop = $('drop');
['dragenter', 'dragover'].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); }),
);
['dragleave', 'drop'].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }),
);
drop.addEventListener('drop', (ev) => setFile(ev.dataTransfer.files[0]));
$('file').addEventListener('change', (ev) => setFile(ev.target.files[0]));
$('clearFile').addEventListener('click', (ev) => { ev.preventDefault(); setFile(null); });

function setFile(f) {
  if (f && !f.type.startsWith('video/')) return alert('File harus berupa video.');
  videoFile = f || null;
  $('dropIdle').hidden = !!videoFile;
  $('dropFile').hidden = !videoFile;
  if (videoFile) {
    $('preview').src = URL.createObjectURL(videoFile);
    $('fileName').textContent = videoFile.name;
    $('fileMeta').textContent = `${(videoFile.size / 1024 / 1024).toFixed(1)} MB · ${videoFile.type}`;
  } else {
    $('file').value = '';
  }
  updateSendState();
}

function updateSendState() {
  $('send').disabled = !(videoFile && selected.size);
  $('sendHint').textContent = !videoFile
    ? 'Pilih video dulu.'
    : selected.size === 0
      ? 'Pilih minimal satu platform tujuan.'
      : `Akan dikirim ke ${selected.size} platform.`;
}

/* ───────────── Upload ───────────── */

$('send').onclick = async () => {
  const fd = new FormData();
  fd.append('video', videoFile);
  fd.append('title', $('title').value);
  fd.append('caption', $('caption').value);
  fd.append('platforms', JSON.stringify([...selected]));
  fd.append('options', JSON.stringify({
    youtubePrivacy: $('ytPrivacy').value,
    tiktokPrivacy: $('ttPrivacy').value,
  }));

  $('send').disabled = true;
  $('progressCard').hidden = false;
  $('mediaBar').hidden = false;
  $('progress').innerHTML = '';
  setMediaBar(0);

  try {
    const { jobId } = await postWithProgress('/api/upload', fd, (pct) => {
      setMediaBar(pct);
      $('send').textContent = `Mengirim ke server… ${pct}%`;
    });
    setMediaBar(100);
    watchJob(jobId);
  } catch (err) {
    $('progress').innerHTML = `<p class="err">❌ ${esc(err.message)}</p>`;
    $('send').disabled = false;
  }
  $('send').textContent = 'Upload ke platform terpilih';
};

// XHR dipakai karena fetch tidak bisa melaporkan progres upload.
function postWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* biarkan kosong */ }
      xhr.status < 400 ? resolve(body) : reject(new Error(body.error || `Gagal (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('Koneksi ke server gagal.'));
    xhr.send(formData);
  });
}

function watchJob(jobId) {
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.onmessage = (ev) => {
    const job = JSON.parse(ev.data);
    renderProgress(job);
    if (job.status !== 'running') {
      es.close();
      $('send').disabled = false;
    }
  };
  es.onerror = () => es.close();
}

const LABEL = { queued: 'antre', running: 'proses', success: 'sukses', error: 'gagal' };
const esc = (s) => String(s).replace(/</g, '&lt;');

function setMediaBar(pct) {
  $('mediaPct').textContent = `${pct}%`;
  $('mediaFill').style.width = `${pct}%`;
}

function renderProgress(job) {
  $('progress').innerHTML = '';
  for (const t of Object.values(job.targets)) {
    const meta = platforms.find((p) => p.id === t.platform) || { name: t.platform, color: '#888' };
    const detail = t.result?.url
      ? `<a href="${t.result.url}" target="_blank" rel="noreferrer">buka post ↗</a>`
      : t.error
        ? `<span class="err">${esc(t.error)}</span>`
        : '';

    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `
      <div class="row-head">
        <span class="dot" style="background:${meta.color}"></span>
        <b>${meta.name}</b>
        <span class="detail">${detail}</span>
        <span class="status ${t.status}">${LABEL[t.status] || t.status}</span>
      </div>
      <div class="bar"><i style="width:${t.progress || 0}%"></i></div>
      <pre class="logs">${esc(t.logs.join('\n') || 'menunggu…')}</pre>`;
    $('progress').appendChild(row);
  }
  // Log terbaru selalu terlihat.
  $('progress').querySelectorAll('.logs').forEach((l) => (l.scrollTop = l.scrollHeight));
}

boot();
