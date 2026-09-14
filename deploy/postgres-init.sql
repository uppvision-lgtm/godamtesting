-- Skema database untuk VPS yang berdiri sendiri (tanpa Supabase).
-- Dijalankan otomatis oleh container Postgres saat volume masih kosong.
-- Isinya sengaja dibuat sama persis dengan tabel app_users di Supabase
-- supaya kode backend tidak perlu tahu sedang bicara ke yang mana.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- menyediakan gen_random_uuid()

CREATE TABLE IF NOT EXISTS public.app_users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

COMMENT ON TABLE public.app_users IS
    'Akun login app instagram-bot. Password disimpan sebagai hash scrypt, bukan teks asli.';

-- Peran yang dipakai PostgREST. Tidak diberi password: PostgREST masuk lewat
-- koneksi internal Docker, dan port-nya tidak pernah dibuka ke internet.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_anon') THEN
        CREATE ROLE web_anon NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO web_anon;
GRANT SELECT, INSERT, UPDATE ON public.app_users TO web_anon;
