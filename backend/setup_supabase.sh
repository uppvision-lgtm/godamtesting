#!/usr/bin/env bash
# Isi kredensial Supabase ke backend/.env tanpa kuncinya terlihat di layar
# maupun tersimpan di riwayat perintah.
set -euo pipefail
cd "$(dirname "$0")"

URL="${SUPABASE_URL_BARU:-https://oxeefrvvvhrantaztwyt.supabase.co}"
BUCKET="${SUPABASE_BUCKET_BARU:-media}"

echo "Ambil kuncinya di: Supabase Dashboard -> proyek TUKANG HACK"
echo "  Project Settings -> API Keys -> service_role (bertanda secret)"
echo
read -rsp "Tempel service_role key lalu Enter: " KEY
echo
if [ -z "${KEY// }" ]; then
  echo "Kunci kosong, tidak ada yang diubah."
  exit 1
fi

touch .env
# Buang baris SUPABASE lama supaya tidak dobel, lalu tulis yang baru.
grep -v -E '^SUPABASE_(URL|SERVICE_KEY|BUCKET)=' .env > .env.baru || true
{
  cat .env.baru
  echo "SUPABASE_URL=$URL"
  echo "SUPABASE_SERVICE_KEY=$KEY"
  echo "SUPABASE_BUCKET=$BUCKET"
} > .env
rm -f .env.baru
chmod 600 .env

echo "Tersimpan di backend/.env (panjang kunci: ${#KEY} karakter)."
echo "Sekarang bilang ke Claude: 'sudah' — backend akan dimuat ulang dan diuji."
