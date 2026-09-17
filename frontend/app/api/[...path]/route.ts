import { NextRequest, NextResponse } from "next/server";

const backendUrl = process.env.BACKEND_API_URL || "http://localhost:8000";

// Stream statistik realtime (SSE) butuh koneksi yang hidup lama, bukan fungsi
// yang selesai dalam sekejap.
export const dynamic = "force-dynamic";
export const maxDuration = 60;

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const search = request.nextUrl.search;
  const destination = `${backendUrl.replace(/\/$/, "")}/api/${path.join("/")}${search}`;
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);
  // Wajib diteruskan: tanpa ini sesi login tidak bisa dipulihkan setelah
  // halaman dimuat ulang, karena /api/auth/me tidak menerima tokennya.
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("authorization", authorization);
  // Identitas tamu (orang yang belum masuk). Tanpa ini, tamu tidak bisa
  // membuat atau menyunting templatenya sendiri.
  const guestId = request.headers.get("x-guest-id");
  if (guestId) headers.set("x-guest-id", guestId);
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) headers.set("x-forwarded-for", forwardedFor);
  try {
    const response = await fetch(destination, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      // Saat tab pengunjung ditutup, permintaan ini ikut dibatalkan sehingga
      // backend langsung tahu koneksinya putus (dipakai hitungan "online").
      signal: request.signal,
    });
    const responseType = response.headers.get("content-type") || "application/json";

    // SSE diteruskan apa adanya sebagai aliran; kalau ditunggu sampai selesai
    // (arrayBuffer) angkanya tidak akan pernah sampai ke browser.
    if (responseType.includes("text/event-stream")) {
      return new NextResponse(response.body, {
        status: response.status,
        headers: {
          "content-type": responseType,
          "cache-control": "no-cache, no-transform",
          connection: "keep-alive",
          "x-accel-buffering": "no",
        },
      });
    }

    // 204/304 dan respons tanpa isi tidak boleh punya body (endpoint /frame
    // memakai 204 saat frame belum siap) — kalau dipaksa, Next akan error dan
    // membalas 502.
    const payload = await response.arrayBuffer();
    if (response.status === 204 || response.status === 304 || payload.byteLength === 0) {
      return new NextResponse(null, { status: response.status });
    }
    return new NextResponse(payload, {
      status: response.status,
      headers: { "content-type": responseType },
    });
  } catch (error) {
    // Sebabnya ikut disebut. Tanpa ini, "backend mati", "salah alamat", dan
    // "koneksi diputus di tengah jalan" tampak sama persis di halaman, dan
    // tidak ada apa pun di log yang bisa dipakai menelusuri.
    const sebab =
      (error as { cause?: { code?: string } } | undefined)?.cause?.code ||
      (error instanceof Error ? error.name : "");
    console.error(`[proxy] ${request.method} ${destination} gagal:`, error);
    // Saat mengembangkan di laptop, alamat tujuan ikut disebut supaya salah
    // isi BACKEND_API_URL langsung ketahuan. Di produksi alamat internal
    // tidak dibocorkan ke pengunjung.
    const tujuan = process.env.NODE_ENV === "production" ? "" : ` -> ${destination}`;
    return NextResponse.json(
      { detail: `Backend FastAPI tidak merespons${sebab ? ` (${sebab})` : ""}.${tujuan}` },
      { status: 502 }
    );
  }
}

export const GET = proxy;
export const POST = proxy;
// Tanpa ini, permintaan DELETE ditolak 405 sebelum sampai ke backend —
// itulah sebabnya tombol hapus set layer selalu gagal.
export const DELETE = proxy;
export const PUT = proxy;
export const PATCH = proxy;
