-- Skema Supabase untuk godamtesting (backend/db.py memakai REST dengan service_role).
-- Disusun ulang dari kode pada 2 Okt 2026 setelah proyek lama (TUKANG HACK) terhapus.
-- Bucket Storage tidak perlu dibuat di sini: storage.ensure_bucket() membuatnya sendiri.
-- Aman dijalankan ulang.

create table if not exists public.app_users (
  id uuid primary key default gen_random_uuid(),
  username text not null unique,
  password_hash text not null,
  sesi integer not null default 0,
  peran text not null default 'pengguna',
  created_at timestamptz not null default now(),
  last_login_at timestamptz
);

create table if not exists public.app_forms (
  username text not null,
  halaman text not null,
  isi text,
  updated_at timestamptz not null default now(),
  primary key (username, halaman)
);

-- Hanya backend (service_role) yang boleh membaca/menulis; kunci anon ditolak.
alter table public.app_users enable row level security;
alter table public.app_forms enable row level security;
