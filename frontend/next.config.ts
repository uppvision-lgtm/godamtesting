import type { NextConfig } from "next";

// Situs ini STATIS penuh ("export" -> folder out/), disajikan Cloudflare Pages.
// Tidak ada lagi bagian yang berjalan di server: semua permintaan data langsung
// ke backend Railway (lihat urlApi di app/components/auth.ts). Proxy /api
// serverless di Vercel sudah dibuang - kuota fungsi paket gratisnya habis dan
// situsnya dihentikan (30 Sep 2026).
//
// Mode "standalone" tetap ada untuk image Docker VPS (Dockerfile menyalin
// .next/standalone).
const nextConfig: NextConfig = {
  output: process.env.BUILD_STANDALONE === "1" ? "standalone" : "export",
};

export default nextConfig;
