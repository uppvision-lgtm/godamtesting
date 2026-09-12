import asyncio
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from celery.exceptions import MaxRetriesExceededError
from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from celery_app import celery_app
from browser_config import browser_args, viewport
from comment_ai import TONES, generate_comments as generate_ai_comments, normalize_tone


logger = logging.getLogger(__name__)
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "10000"))
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", "30000"))


class RetryableBotError(RuntimeError):
    """A transient error that should be retried by Celery."""


class BotExecutionError(RuntimeError):
    """A permanent or input-related bot error."""
STATE_DIR = Path(os.getenv("COMMENT_STATE_DIR", ".comment-state"))
INSTAGRAM_BASE_URL = "https://www.instagram.com"
INSTAGRAM_LOGIN_URL = f"{INSTAGRAM_BASE_URL}/accounts/login/"
DELAY_MIN = float(os.getenv("DELAY_MIN", "5"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "12"))


def _state_path(username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
    return STATE_DIR / f"commented_state_{safe_username}.json"


def _load_commented_state(username: str) -> dict[str, int]:
    path = _state_path(username)
    try:
        with path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
        if isinstance(state, dict):
            return {url: int(count) for url, count in state.get("commented_posts", {}).items()}
        return {url: 1 for url in state} if isinstance(state, list) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, AttributeError, TypeError) as error:
        logger.warning("Invalid comment state at %s: %s", path, error)
        return {}


def _save_commented_state(username: str, commented_posts: dict[str, int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(username)
    temporary_path = path.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump({"commented_posts": commented_posts}, state_file, indent=2)
    temporary_path.replace(path)


def _target_url(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target.rstrip("/")
    handle = target.strip().lstrip("@")
    return f"{INSTAGRAM_BASE_URL}/{quote(handle, safe='')}/"


# ============================================================
#  KOMENTAR: DeepSeek (utama) + BANK LOKAL (cadangan per tone)
#  Tone yang didukung: positif (default), netral, negatif.
#  Komentar selalu relevan dgn caption, tanpa hashtag, tanpa batas jumlah.
# ============================================================

FALLBACK_BANK: dict[str, list[str]] = {
    "positif": [
        "keren banget, thanks udah share.",
        "informasi yang bermanfaat, aku suka.",
        "wah, ini bikin penasaran.",
        "seru banget, lanjutkan.",
        "ini penting banget, wajib tahu.",
    ],
    "netral": [
        "info ini lumayan, ditunggu update berikutnya.",
        "jadi lebih paham setelah nonton ini.",
        "nonton sampai habis, isinya lumayan jelas.",
        "menarik, topik ini memang sedang ramai dibahas.",
        "boleh dijelaskan lagi bagian yang tadi?",
    ],
    "negatif": [
        "sayangnya isinya masih kurang lengkap.",
        "menurutku penjelasannya masih terlalu dangkal.",
        "harusnya bagian ini dibahas lebih detail.",
        "masih banyak yang belum jelas, semoga ada lanjutannya.",
        "cukup disayangkan dibahas setengah-setengah.",
    ],
}


def fallback_comment(tone: str = "positif") -> str:
    tone = normalize_tone(tone)
    return random.choice(FALLBACK_BANK.get(tone) or FALLBACK_BANK["positif"])


def fallback_comments(reason: str = "", count: int = 3, tone: str = "positif") -> list[str]:
    if reason:
        logger.info("Fallback komentar (%s) karena: %s", normalize_tone(tone), reason)
    try:
        count = max(0, int(count))
    except Exception:
        count = 3
    return [fallback_comment(tone) for _ in range(count)]


# Template cadangan untuk tone netral & negatif (pakai {S} = subjek caption).
_TONE_TEMPLATES: dict[str, list[str]] = {
    "netral": [
        "info soal {S} ini lumayan, ditunggu update selanjutnya.",
        "jadi tahu lebih banyak soal {S} habis nonton ini.",
        "bahasan {S} di sini cukup jelas.",
        "baru tahu soal {S} dari postingan ini.",
        "menarik, {S} memang lagi sering dibahas belakangan ini.",
        "nonton sampai habis, soal {S} jadi lebih kebayang.",
        "sekadar menambahkan, {S} juga sempat diberitakan di tempat lain.",
        "boleh dijelaskan lagi bagian {S}-nya?",
        "catatan aja soal {S}, semoga updatenya rutin.",
        "oke, {S} masuk daftar yang mau aku ikuti.",
        "terima kasih infonya soal {S}.",
        "menarik soal {S}, tapi masih pengen tahu detailnya.",
    ],
    "negatif": [
        "sayangnya penjelasan soal {S} masih kurang lengkap.",
        "menurutku bahasan {S} di sini masih terlalu dangkal.",
        "harusnya soal {S} dibahas lebih detail, bukan cuma sekilas.",
        "kurang setuju sama cara penyampaian {S} di sini.",
        "masih banyak yang belum jelas soal {S}, semoga ada lanjutannya.",
        "kayaknya perlu data yang lebih kuat soal {S}.",
        "judulnya soal {S} tapi isinya belum menjawab rasa penasaran.",
        "semoga ke depannya pembahasan {S} lebih jelas dan tidak membingungkan.",
        "cukup disayangkan {S} dibahas setengah-setengah.",
        "menurutku bagian {S} perlu dikoreksi, masih ada yang meleset.",
        "tunggu update berikutnya soal {S}, semoga lebih rapi.",
        "semoga {S} tidak cuma jadi konten sesaat.",
    ],
}


async def generate_comments_from_bank(caption: str, count: int = 6, tone: str = "positif") -> list[str]:
    """Hasilkan komentar sebanyak ``count`` dari bank lokal (tanpa API eksternal).

    Dipakai sebagai cadangan kalau DeepSeek tidak tersedia/gagal.

    Logika salinan dari ``fix copy 2.py`` (generate_comments_from_bank):
    - analisis kata kunci caption (hashtag hanya sinyal, tak ikut menempel);
    - deteksi mood konten (concern/celebrate/tips/music/food/travel/live/news)
      untuk tone positif (default);
    - tone netral/negatif memakai template khusus agar nadanya tidak meleset;
    - komentar dijamin unik & diisi sampai penuh lewat bank cadangan.
    """
    tone = normalize_tone(tone)
    try:
        count = max(0, int(count))
    except Exception:
        count = 6
    if not caption or len(caption) < 3:
        logger.info("Caption kosong atau terlalu pendek: %r", caption)
        return fallback_comments("caption kosong", count, tone)

    # Bersihkan caption; buang tanda # agar tag tidak ikut menempel di komentar.
    cleaned = re.sub(r"\s+", " ", caption).strip()

    # --- Analisis kata kunci topik (tanpa hashtag, tanpa kata tugas) ---
    hashtag_raw = re.findall(r"#([A-Za-z0-9]+)", cleaned)
    no_tags_text = re.sub(r"#\w+", " ", cleaned)  # hapus kata hashtag dari teks

    raw_tokens = re.findall(r"[A-Za-z0-9]+", no_tags_text)
    low_tokens = [t.lower() for t in raw_tokens]

    stop_words = {
        # Kata tugas/fungsi + kata pembuka judul berita yang bukan topik.
        "yang", "di", "ke", "dari", "dan", "ini", "itu", "untuk", "dengan",
        "saat", "juga", "bisa", "lebih", "mau", "tapi", "semua", "jadi",
        "dalam", "tentang", "akan", "ada", "sama", "baik", "serta", "karena",
        "oleh", "maka", "para", "deh", "ya", "ga", "gak", "lagi", "sudah",
        "sekali", "aja", "nih", "si", "gue", "aku", "kita", "mereka",
        "suatu", "tidak", "dapat", "harus", "ingin", "supaya", "agar",
        "sehingga", "namun", "tetapi", "sedangkan", "atau", "pada", "antara",
        "setiap", "beberapa", "tanpa", "setelah", "sebelum", "sebagai",
        "selama", "ketika", "melalui", "mengenai", "terkait", "terhadap",
        "kembali", "masih", "belum", "hanya", "cuma", "pula", "tersebut",
        "adalah", "secara", "sangat", "paling", "terus", "jangan",
        "berarti", "menjadi", "merupakan", "seluruh", "banyak", "bagian",
        "heboh", "viral", "breaking", "news", "update", "info", "alert",
        "cek", "simak", "tonton", "saksikan", "wow", "waw", "baru", "penting",
        "sumber", "instagram", "tiktok", "youtube", "facebook", "repost",
        "official", "video", "caption", "postingan", "langsung", "hari",
        "pada", "kata", "serta", "tentang",
    }

    # Filter hashtag milik kanal/media (bukan topik).
    def is_brand_tag(tag: str) -> bool:
        low = tag.lower()
        return low.startswith("tv") or low in (
            "tvrakyat", "tvri", "tvgenmedia", "tvone", "metrotv", "kompas",
            "instagram", "tiktok", "youtube", "sumber", "official",
        )

    hashtags = [h for h in hashtag_raw if not is_brand_tag(h)]
    hashtag_lows = [h.lower() for h in hashtags]

    body_tokens = []
    seen = set()
    for tok, low in zip(raw_tokens, low_tokens):
        if len(low) > 2 and low not in stop_words and not low.isdigit() and low not in seen:
            seen.add(low)
            body_tokens.append((tok, low))

    if not body_tokens:
        return fallback_comments("caption terlalu pendek", count, tone)

    # Skor tiap kata kunci: makin cocok dgn hashtag & nama/entitas = makin relevan.
    role_words = {
        "menteri", "presiden", "gubernur", "wakil", "bupati", "walikota", "kepala",
        "ketua", "jenderal", "panglima", "dinas", "kapolri", "mentri", "pemerintah",
    }
    scored = []
    for idx, (tok, low) in enumerate(body_tokens):
        score = 0
        if any(h == low or h.startswith(low) or low.startswith(h) for h in hashtag_lows):
            score += 4          # kata yang juga jadi hashtag = sinyal topik kuat
        if tok[0].isupper():
            score += 1          # nama/entitas
        if len(low) >= 6:
            score += 1          # kata panjang cenderung lebih spesifik
        if low in role_words:
            score -= 2          # jabatan umum jangan sampai menenggelamkan topik spesifik
        scored.append((score, idx, tok, low))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [tok for _, _, tok, _ in scored]
    k1 = picked[0]
    k2 = picked[1] if len(picked) > 1 else k1
    k3 = picked[2] if len(picked) > 2 else k2
    K1 = k1.capitalize()

    # --- Deteksi nuansa konten (berbasis kata utuh, bukan substring) ---
    token_set = set(low_tokens)

    def has_any(words: set[str]) -> bool:
        return bool(set(words) & token_set)

    if has_any({"duka", "berduka", "wafat", "meninggal", "gugur", "musibah", "belasungkawa", "korban jiwa"}):
        mood = "concern"
    elif has_any({"selamat", "juara", "menang", "lulus", "prestasi", "bangga", "raya", "ulang tahun", "sukses"}):
        mood = "celebrate"
    elif has_any({"tips", "tutorial", "cara", "trik", "guide", "step", "resep", "praktik"}):
        mood = "tips"
    elif has_any({"musik", "lagu", "song", "beat", "dance", "audio", "nyanyi"}):
        mood = "music"
    elif has_any({"makanan", "food", "kuliner", "masak", "cooking", "lezat"}):
        mood = "food"
    elif has_any({"travel", "wisata", "liburan", "trip", "destinasi", "pantai", "gunung"}):
        mood = "travel"
    elif has_any({"live", "stream", "siaran", "event", "konser"}):
        mood = "live"
    else:
        # Default untuk kanal berita/informasi: tetap positif & mendukung.
        mood = "news"

    # --- Varian subjek agar komentar bervariasi (bukan cuma 1 kata) ---
    subjects = []

    def _add_subj(s: str) -> None:
        if s and s.lower() not in {x.lower() for x in subjects}:
            subjects.append(s)

    _add_subj(k1)
    _add_subj(k2)
    _add_subj(k3)
    if k2 != k1:
        _add_subj(f"{k1} dan {k2}")

    # --- Bank template per mode/tone (pakai {S} = subjek) ---
    if tone != "positif":
        templates = _TONE_TEMPLATES[tone]
    elif mood == "concern":
        templates = [
            "Turut berduka dan mendoakan yang terbaik, semoga semua yang ditinggalkan diberi ketabahan.",
            "Semoga semua yang terdampak {S} diberi kekuatan dan lekas pulih.",
            "Terima kasih sudah berbagi kabar ini, semoga pemulihannya berjalan lancar.",
            "Dukungan dan doa terbaik untuk semua yang menghadapi cobaan ini.",
            "Semoga suasana segera membaik dan semua tetap dalam lindungan.",
            "Terima kasih atas informasinya, semoga kebersamaan meringankan beban sesama.",
            "Semoga para pihak yang membantu {S} selalu diberi kelancaran.",
            "Semoga keadaan {S} segera kondusif dan semua aman.",
            "Kita doakan yang terbaik untuk semua yang sedang melalui {S}.",
            "Semoga langkah pemulihan {S} berjalan lancar dan membawa kebaikan.",
            "Semangat untuk semua yang berjuang membantu {S}, kita dukung penuh.",
            "Semoga semua tetap kuat dan sehat, kita berdoa yang terbaik.",
        ]
    elif mood == "celebrate":
        templates = [
            "Selamat dan bangga banget atas {S}, semoga makin sukses ke depannya.",
            "MasyaAllah keren, {S} ini patut diapresiasi dan dijadikan teladan.",
            "Suka banget lihat {S} seperti ini, semoga terus berlanjut.",
            "Semangat dan sukses selalu untuk {S}, hasilnya terlihat jelas.",
            "{S} ini luar biasa, semoga membawa berkah dan menginspirasi.",
            "Alhamdulillah, selamat atas {S}! Semoga jadi awal pencapaian lebih besar.",
            "Bangga banget sama {S}, kerja keras memang tidak menghianati hasil.",
            "Semoga {S} terus naik level dan makin membanggakan.",
            "Suka banget, {S} jadi bukti bahwa usaha tidak pernah sia-sia.",
            "Keren abis, semoga {S} memotivasi banyak orang.",
            "Selamat dan sukses terus untuk {S}, doa terbaik menyertai.",
            "MasyaAllah tabarakallah, {S} bikin makin semangat.",
        ]
    elif mood == "tips":
        templates = [
            "Terima kasih tips soal {S}-nya, jelas dan mudah dipraktikkan.",
            "Penjelasan {S} di sini gampang diikuti, semoga makin banyak konten bermanfaat.",
            "Worth it banget, {S} yang dibahas ternyata sangat membantu.",
            "Suka cara penyampaiannya, {S} jadi terasa simpel.",
            "Semoga makin sering berbagi seputar {S}, banyak yang terbantu.",
            "Makasih sharingnya soal {S}, langsung bisa dipakai.",
            "Tips {S} ini keren, langsung masuk catatan.",
            "Praktis banget, {S} jadi tidak ribet untuk dicoba.",
            "Semoga terus ada konten {S} yang seperti ini.",
            "Penjelasan {S}-nya runtut dan gampang dipahami.",
            "Terima kasih, {S} ini sangat berguna untuk sehari-hari.",
            "Suka banget, semoga tips {S} ini banyak yang menerapkan.",
        ]
    elif mood == "music":
        templates = [
            "Vibes {S} di video ini enak banget, bikin semangat seharian.",
            "Musik dan visualnya padu banget, jadi makin betah nonton.",
            "Suka banget suasananya, {S} bikin suasana makin hidup.",
            "Keren banget, tipe konten yang bikin mood langsung naik.",
            "Enak didengar dan dinikmati, semoga makin banyak karya seperti {S}.",
            "Bikin adem dan happy, cocok jadi teman beraktivitas.",
            "Sound {S} ini bikin merinding kerennya.",
            "Gak berasa nontonnya, {S} bikin betah.",
            "Semoga makin sering rilis karya senyaman {S} ini.",
            "Mantap jiwa, {S} bikin suasana makin berwarna.",
            "Suka banget beat-nya, {S} cocok buat diputar berulang.",
            "Konten {S} ini bikin hari makin ceria.",
        ]
    elif mood == "food":
        templates = [
            "Kelihatan lezat banget, {S} bikin pengen langsung mencoba.",
            "Suka banget tampilannya, menggugah selera dan bikin ngiler.",
            "Semoga {S} ini bisa dicoba di rumah, terlihat simpel dan enak.",
            "Kontennya bikin semangat masak, apalagi bahas soal {S}.",
            "Enak banget kayaknya, semoga bisa recook dan hasilnya seenak ini.",
            "Makin penasaran sama {S}-nya, terlihat fresh dan menggoda.",
            "Bikin laper, {S} ini kelihatan juara banget.",
            "Suka banget, {S} jadi pengen langsung cari tahu resepnya.",
            "Semoga makin banyak rekomendasi {S} yang seenak ini.",
            "Tampilannya cantik dan menggoda, {S} ini wajib dicoba.",
            "Ditunggu menu {S} lainnya, selalu ditunggu kontennya.",
            "Mantap, {S} bikin mood makan naik.",
        ]
    elif mood == "travel":
        templates = [
            "View {S} di video ini bikin kangen liburan, masuk wishlist.",
            "Keindahan {S} ini keren banget, semoga bisa ke sana suatu hari.",
            "Semoga makin banyak yang tahu tempat sebagus {S}.",
            "Bikin adem dan betah, {S} memang layak dikunjungi.",
            "Kontennya memanjakan mata, apalagi soal {S}.",
            "Suka banget, {S} jadi makin pengin dieksplor.",
            "Wishlist bertambah nih, {S} kelihatan damai banget.",
            "Semoga {S} makin ramai dikunjungi orang baik-baik.",
            "Foto dan videonya bikin betah, {S} juara.",
            "Suka banget, semoga bisa healing ke {S}.",
            "Bikin pengen segera traveling, {S} nampak menawan.",
            "Ditunggu rekomendasi {S} lainnya yang sebagus ini.",
        ]
    elif mood == "live":
        templates = [
            "Acara {S} ini seru banget, sayang kalau sampai terlewat.",
            "Vibes-nya meriah dan positif, semoga acaranya makin sukses.",
            "Makin penasaran dengan {S}, ditunggu keseruan selanjutnya.",
            "Keren, semoga {S} makin ramai dan lancar sampai selesai.",
            "Enak banget ditonton, {S} bikin betah sampai akhir.",
            "Semoga acara {S} dinikmati banyak orang dan berjalan lancar.",
            "Seru abis, {S} bikin pengen ikut meramaikan.",
            "Ditunggu keseruan {S} berikutnya, pasti makin meriah.",
            "Mantap, semoga {S} ini sukses dan menginspirasi.",
            "Suka banget atmosfernya, {S} bikin semangat.",
            "Semoga {S} makin banyak penonton yang terhibur.",
            "Keren banget, {S} bikin gak mau ketinggalan.",
        ]
    else:  # news / informasi / umum
        templates = [
            "Terima kasih infonya, makin paham soal {S} setelah nonton ini.",
            "Berita soal {S} penting banget, apresiasi buat media yang mengangkatnya.",
            "Semoga perkembangan {S} terus membaik dan membawa manfaat.",
            "Nonton sampai habis, penjelasan soal {S} bikin makin paham.",
            "Makin melek sama {S} setelah ini, terima kasih sudah mengedukasi.",
            "Konten soal {S} disajikan jelas dan menyejukkan.",
            "Semoga kabar {S} ini jadi pengingat untuk berpikir positif.",
            "Info {S} yang fresh begini bermanfaat banget.",
            "Semoga {S} ke depannya makin baik dan kondusif.",
            "Salut dengan penyajian berita {S} yang informatif.",
            "Apresiasi untuk yang terus menyampaikan info {S} dengan jelas.",
            "Semoga {S} terus mendapat perhatian dan ditindaklanjuti dengan baik.",
            "Terima kasih edukasinya seputar {S}, sangat mencerahkan.",
            "Makin respect sama media yang mengangkat {S} begini.",
            "Semoga {S} membawa dampak positif untuk semua orang.",
            "Bagus banget, semoga kabar {S} ini makin tersebar luas.",
            "Terima kasih, berita {S} jadi makin gampang dipahami.",
            "Semoga {S} terus diikuti dan memberi pelajaran baik.",
            "Suka banget dengan pembahasan {S} yang berimbang.",
            "Semoga makin banyak info positif seputar {S}.",
            "Makin mantap, semoga {S} terus berkembang dengan baik.",
            "Terima kasih, pembahasan {S} ini sangat membantu.",
            "Semoga {S} menjadi kabar baik yang dinantikan banyak orang.",
            "Suka banget, berita {S} ini layak dibaca sampai habis.",
            "Semoga {S} terus didukung semua pihak agar makin baik.",
            "Apresiasi, semoga {S} makin dikenal dan bermanfaat luas.",
        ]

    # Kata negatif yang tidak boleh muncul di komentar.
    negatif = {
        "buruk", "jelek", "parah", "bodoh", "tolol", "goblok", "korup",
        "korupsi", "gagal", "hancur", "rusak", "benci", "membenci",
        "mengutuk", "mengecam", "kecam", "rugi", "bohong", "penipu",
        "konyol", "sial", "busuk", "curang", "sombong", "kritik",
        "tolak", "menolak", "dendam", "marah", "memaki",
    }

    # Gabungkan template x subjek = banyak variasi komentar unik.
    # Filter kata negatif hanya berlaku untuk tone positif (tone negatif justru
    # memang menyoroti kekurangan, tapi tetap sopan karena template-nya dijaga).
    pool = []
    for tmpl in templates:
        for subj in subjects:
            line = tmpl.format(S=subj)
            if tone == "positif" and any(w in line.lower() for w in negatif):
                continue
            pool.append(line)

    random.shuffle(pool)
    result = []
    seen_result = set()
    for line in pool:
        if len(result) >= count:
            break
        low = line.lower()
        if low in seen_result:
            continue
        seen_result.add(low)
        result.append(line)

    # Bank positif cadangan (dipakai kalau target lebih besar dari variasi unik).
    cadangan = [
        "Kontennya informatif dan menyejukkan, semoga terus berbagi hal baik.",
        "Terima kasih sudah berbagi, semoga bermanfaat untuk banyak orang.",
        "Suka banget dengan cara penyampaiannya, semoga makin sukses.",
        "Semoga hal baik ini terus berlanjut dan menginspirasi.",
        "Keren dan mencerahkan, ditunggu konten menarik lainnya.",
        "Semoga semua yang terlibat selalu diberi kelancaran.",
        "Makin banyak yang terbantu dengan konten seperti ini.",
        "Semoga terus konsisten berbagi hal yang bermanfaat.",
        "Terima kasih, semoga kabar baik ini terus berlanjut.",
        "Suka banget, semoga semakin banyak yang terinspirasi.",
    ]
    random.shuffle(cadangan)
    for line in cadangan:
        if len(result) >= count:
            break
        low = line.lower()
        if low in seen_result:
            continue
        seen_result.add(low)
        result.append(line)

    # Kalau target sangat besar, ulangi bank positif (tanpa batas) sampai penuh.
    if result:
        source = list(result)
        i = 0
        while len(result) < count:
            result.append(source[i % len(source)])
            i += 1

    return result[:count]


async def open_target_profile(page: Page, target: str) -> None:
    try:
        await page.goto(
            _target_url(target),
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RetryableBotError("Instagram profile navigation timed out") from error
    await page.wait_for_timeout(1500)


async def check_page_access(page: Page) -> None:
    current_url = page.url.lower()
    if "/accounts/login" in current_url or "/login" in current_url:
        raise BotExecutionError("Instagram session is expired or invalid")
    # Instagram kadang menampilkan formulir login tanpa mengubah URL, jadi
    # keberadaan kolom sandi ikut diperiksa supaya pesannya tidak menyesatkan.
    try:
        kolom_sandi = page.locator('input[type="password"]').first
        if await kolom_sandi.count() and await kolom_sandi.is_visible():
            raise BotExecutionError("Instagram session is expired or invalid")
    except BotExecutionError:
        raise
    except Exception:
        pass
    body = (await page.locator("body").inner_text(timeout=PLAYWRIGHT_TIMEOUT_MS)).lower()
    if any(marker in body for marker in ("try again later", "please wait a few minutes", "coba lagi nanti")):
        raise RetryableBotError("Instagram rate limit or throttle detected")
    if any(marker in body for marker in ("page isn't available", "user not found", "akun tidak ditemukan")):
        raise BotExecutionError("Instagram target account was not found")


async def close_popups(page: Page) -> None:
    for label in ("Not Now", "Save Info", "Tidak sekarang", "Simpan Info"):
        popup_button = page.locator(f'button:has-text("{label}")').first
        try:
            if await popup_button.is_visible(timeout=500):
                await popup_button.click()
        except PlaywrightTimeoutError:
            continue


_SCAN_GRID_JS = r"""() => {
    // Pindai tile grid profil saat ini; tandai tile yang merupakan postingan
    // disematkan/pinned. Penanda andal: di pojok thumbnail ada ikon svg
    // aria-label "Ikon postingan yang disematkan" (id) / "Pinned ..." (en).
    // Tile reel biasa memakai ikon aria-label "Klip"/"Reel", jadi tak tertukar.
    // Catatan: href grid = "/<akun>/reel/CODE/", bukan "/reel/CODE/".
    function looksPinned(a) {
        for (const el of a.querySelectorAll('svg, img')) {
            const label = (
                (el.getAttribute('aria-label') || '') + ' ' +
                (el.getAttribute('alt') || '') + ' ' +
                (el.textContent || '')
            ).toLowerCase();
            if (/semat|disematkan|sematkan|menyematkan|pinned|pin\b/.test(label)) {
                return true;
            }
        }
        return false;
    }
    const items = [];
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href]')) {
        const rel = (a.getAttribute('href') || '').split('?')[0];
        if (!/\/reel\/|\/p\/|\/tv\//.test(rel)) continue;
        const abs = 'https://www.instagram.com' + rel.replace(/\/$/, '');
        if (seen.has(abs)) continue;
        seen.add(abs);
        items.push({ abs: abs, pinned: looksPinned(a) });
    }
    return items;
}"""


async def collect_posts(
    page: Page,
    limit: int,
    log: Any | None = None,
) -> list[dict[str, str]]:
    """Kumpulkan ``limit`` postingan TERBARU dari profil target.

    Postingan yang disematkan (pinned) di grid profil dilewati, dan penggantinya
    diambil dari postingan berikutnya sehingga jumlah yang dikomentari tetap
    ``limit`` (hanya postingan non-pin).
    """
    ordered: list[str] = []
    pinned_urls: set[str] = set()
    non_pinned_urls: list[str] = []

    for _ in range(25):
        items = await page.evaluate(_SCAN_GRID_JS)
        for item in items:
            url = item["abs"]
            if url in ordered:
                # Bisa saja penanda pin baru muncul setelah scroll; perbarui.
                if item["pinned"]:
                    pinned_urls.add(url)
                continue
            ordered.append(url)
            if item["pinned"]:
                pinned_urls.add(url)
        non_pinned_urls = [u for u in ordered if u not in pinned_urls]
        if len(non_pinned_urls) >= limit:
            break
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(1600)

    if log and pinned_urls:
        log(
            f"Melewati {len(pinned_urls)} postingan disematkan (pin): "
            f"{', '.join(u.rsplit('/', 1)[-1] for u in list(pinned_urls)[:5])}"
        )

    posts: list[dict[str, str]] = []
    for url in non_pinned_urls[:limit]:
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
            )
            caption = await get_caption(page)
            posts.append({"url": url, "caption": caption})
        except Exception as error:
            logger.warning("Could not read post %s: %s", url, error)
    return posts


def _parse_caption_from_meta(meta: str) -> str:
    """Ekstrak caption utuh dari meta description.

    Format meta Instagram:
    '12 likes, 3 comments - username pada 8 September 2026: "isi caption
    paragraf 1

    paragraf 2

    #Tag1 #Tag2". '
    """
    if not meta:
        return ""
    meta = meta.strip()
    # Ambil teks di antara tanda kutip pembuka (setelah ': ') dan kutip
    # penutup terakhir di akhir string (bisa diikuti titik/spasi).
    m = re.search(r':\s*["“](.+?)["”]\s*\.?\s*$', meta, re.S)
    return m.group(1).strip() if m else ""


async def get_caption(page: Page) -> str:
    """Ambil caption LENGKAP (semua paragraf + hashtag) dari postingan.

    Disalin 1:1 dari ``fix copy 2.py``:
    1) Sumber utama: tag <meta name="description"> (Instagram menyimpan caption
       lengkap di sana).
    2) Fallback DOM: kumpulkan semua paragraf caption pada halaman postingan.
    """
    await page.wait_for_timeout(2000)

    # 1) Sumber utama: meta description.
    raw_meta = await page.evaluate(
        """() => {
            const m = document.querySelector('meta[name="description"]')
                || document.querySelector('meta[property="og:description"]');
            return m ? (m.getAttribute('content') || '') : '';
        }"""
    )
    if raw_meta:
        parsed = _parse_caption_from_meta(raw_meta)
        if len(parsed) > 10:
            logger.info("Caption lengkap dari meta description (panjang: %s)", len(parsed))
            return parsed

    # 2) Fallback DOM: kumpulkan SEMUA paragraf caption (bukan baris terpanjang).
    caption = await page.evaluate(
        """
        () => {
            const container = document.querySelector('article') || document.querySelector('main[role="main"]');
            if (!container) return null;

            const allText = container.innerText || container.textContent || '';
            if (!allText) return null;

            const lines = allText.split('\\n')
                .map(l => l.trim())
                .filter(l => l.length > 0);

            const ignorePatterns = [
                /^[0-9,]+ (suka|like|komentar|comment|views|tayangan)$/i,
                /^[0-9]+ (hari|jam|menit|detik|second|minute|hour|day|tahun) (yang lalu|ago|lalu)$/i,
                /^[0-9]+$/,
                /^(muat komentar lainnya|load more comments|lihat semua komentar|lihat postingan lainnya)$/i,
                /^[0-9,]+ suka$/i,
                /^[0-9,]+ komentar$/i
            ];

            // Baris yang jelas bukan bagian caption (awalan komentar dsb).
            const looksLikeComment = (line) => (
                /^(balas|reply|sukai|suka|ikuti|follow|\\u2022)$/i.test(line) ||
                /^\\s*(hari|jam|menit|detik|minggu)\\s*$/i.test(line)
            );

            const candidates = lines.filter(line => {
                if (line.length < 10) return false;
                if (looksLikeComment(line)) return false;
                for (const pattern of ignorePatterns) {
                    if (pattern.test(line)) return false;
                }
                return true;
            });

            if (candidates.length === 0) return null;

            // Gabungkan semua paragraf caption menjadi satu teks utuh.
            return candidates.join(' ');
        }
        """
    )

    if caption and len(caption.strip()) > 10:
        logger.info("Caption lengkap dari DOM (panjang: %s)", len(caption.strip()))
        return caption.strip()

    logger.info("Caption tidak ditemukan")
    return ""


# ---- Auto-Like: pendekatan berbasis elemen (bukan selector CSS) ----
# IG memakai <div role="button"> ber-ikon svg[aria-label="Suka"] untuk tombol
# Like postingan (reel bisa tampil tanpa <article>). Karena banyak ikon "Suka"
# lain (grid/overlay), tombol Like utama dicari lewat tombol aksi
# "Bagikan/Komentari" sebagai penanda bar aksi yang sama. (dari fix copy 2.py)

_FIND_MAIN_LIKE_JS = """() => {
    const LIKES = ['suka', 'like', 'tidak suka', 'unlike', 'batal suka'];
    const ACTIONS = ['bagikan', 'share', 'kirim', 'send', 'komentari', 'comment', 'simpan', 'save'];
    const ilabel = (el) => (el.getAttribute('aria-label') || el.getAttribute('alt') || '').toLowerCase();
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
            && s.display !== 'none' && r.top < innerHeight && r.bottom > 0;
    };

    // Semua ikon Like yang tampak (svg/img apa pun, tidak di dalam <a>).
    const likes = [...document.querySelectorAll('svg[aria-label], img[alt]')]
        .filter(ic => LIKES.includes(ilabel(ic)) && visible(ic) && !ic.closest('a'));

    // Ikon aksi penanda bar aksi, urut prioritas Bagikan/Kirim > Komentari > Simpan.
    const actIcons = [...document.querySelectorAll('svg[aria-label], img[alt]')]
        .filter(ic => ACTIONS.includes(ilabel(ic)) && visible(ic));
    actIcons.sort((a, b) => {
        const pa = ['bagikan','kirim','share','send','komentari','comment','simpan','save'].indexOf(ilabel(a));
        const pb = ['bagikan','kirim','share','send','komentari','comment','simpan','save'].indexOf(ilabel(b));
        return pa - pb;
    });

    // Pilih Like yang berbagi ancestor PALING DALAM dengan ikon aksi
    // (= tombol Like di bar aksi reel yang sama, bukan ikon mini reel lain).
    let best = null, bestDepth = 1e9;
    for (const act of actIcons) {
        const actAnc = [];
        let n = act.parentElement;
        while (n && n !== document.documentElement) { actAnc.push(n); n = n.parentElement; }
        for (const like of likes) {
            let d = -1, node = like;
            while (node && node !== document.documentElement) {
                const i = actAnc.indexOf(node);
                if (i >= 0) { d = i; break; }
                node = node.parentElement;
            }
            // indeks kecil = ancestor bersama paling dalam = paling dekat dengan bar aksi
            if (d >= 0 && d < bestDepth) { bestDepth = d; best = like; }
        }
        if (best) break;
    }

    // Fallback: ikon Like visible terbesar (bukan ikon mini).
    if (!best) {
        likes.sort((a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            return (rb.width * rb.height) - (ra.width * ra.height);
        });
        best = likes[0] || null;
    }
    return best;
}"""

# Baca status tombol Like yang sudah ditemukan (ikon bisa <svg> atau <img>).
_LIKE_STATE_JS = """(el) => {
    const icon = el.querySelector('svg[aria-label], img[alt]');
    const label = (icon
        ? icon.getAttribute('aria-label') || icon.getAttribute('alt')
        : el.getAttribute('aria-label') || el.getAttribute('alt')) || '';
    return {
        label: label,
        pressed: el.getAttribute('aria-pressed') || '',
        tag: el.tagName
    };
}"""


async def _find_like_element(page: Page):
    """Cari elemen tombol Like di bar aksi reel/postingan aktif."""
    try:
        handle = await page.evaluate_handle(_FIND_MAIN_LIKE_JS)
        return handle.as_element()  # None jika tidak ketemu
    except Exception:
        return None


async def like_if_not_liked(page: Page) -> bool:
    """Like postingan (bukan komentar) & VERIFIKASI statusnya jadi aktif.

    Bar aksi reel sering termount beberapa detik setelah halaman load, jadi
    tombol dicari berulang dengan jeda sebelum menyerah. Logika disalin 1:1
    dari ``fix copy 2.py`` (like_if_not_liked).
    """
    if "login" in page.url:
        logger.info("Tidak bisa like: sesi Instagram belum login atau sudah kedaluwarsa.")
        return False

    # Tunggu sampai bar aksi termount (retry + scroll pelan).
    like_el = None
    for attempt in range(10):
        like_el = await _find_like_element(page)
        if like_el is not None:
            break
        if attempt in (3, 6):
            try:
                await page.evaluate("window.scrollBy(0, 300)")
            except Exception:
                pass
        await page.wait_for_timeout(1500)

    if like_el is None:
        # Cek apakah ini karena belum login (bukan karena selector).
        try:
            login_prompt = await page.evaluate(
                """() => {
                    const t = (document.body.innerText || '').toLowerCase();
                    return t.includes('untuk menyukai') || t.includes('log in to like')
                        || t.includes('masuk untuk menyukai');
                }"""
            )
        except Exception:
            login_prompt = False
        if login_prompt:
            logger.info("Tidak bisa like: sesi belum aktif (muncul ajakan login di halaman).")
            return False
        logger.info("Tombol Like postingan tidak ditemukan (bar aksi belum muncul).")
        return False

    state = await page.evaluate(_LIKE_STATE_JS, like_el)
    label = (state.get("label") or "").lower()
    if state.get("pressed") == "true" or any(u in label for u in ("tidak suka", "unlike", "batal suka")):
        logger.info("Sudah di-like. Lewati.")
        return True

    await like_el.scroll_into_view_if_needed()
    await like_el.click()

    # Verifikasi: cari ulang tombol Like di bar aksi yang sama (React bisa
    # mengganti DOM setelah klik, dan ada ikon mini reel lain di DOM).
    liked = False
    for _ in range(12):
        await page.wait_for_timeout(500)
        el2 = await _find_like_element(page)
        if el2 is None:
            continue
        try:
            st = await page.evaluate(_LIKE_STATE_JS, el2)
            l = (st.get("label") or "").lower()
            if st.get("pressed") == "true" or any(u in l for u in ("tidak suka", "unlike", "batal suka")):
                liked = True
                break
        except Exception:
            continue

    if liked:
        logger.info("Berhasil like postingan!")
        return True
    logger.info("Like diklik, tetapi status aktif belum dapat diverifikasi.")
    return False


async def hitung_komentar_saya(page: Page, username: str) -> int:
    """Hitung komentar yang benar-benar dibuat oleh akun ``username``.

    Setiap komentar di Instagram selalu memiliki link "permalink" ke komentar
    itu, polanya ``.../c/<id>/``. Link inilah penanda unik satu komentar. Kita
    hitung satu komentar jika di dalam blok komentar yang sama ada link ke
    profil ``username`` dengan teks persis sama (bukan sekadar mention
    ``@username`` di komentar orang lain). (dari fix copy 2.py)
    """
    # Gulir pelan supaya komentar termuat (terutama yang butuh lazy-load).
    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(1200)

    count = await page.evaluate(
        """
        ({ username }) => {
            const target = username.toLowerCase().replace(/^@/, '');
            const blocks = new Set();

            document.querySelectorAll('a[href*="/c/"]').forEach((perm) => {
                let node = perm;
                for (let depth = 0; depth < 10 && node; depth++) {
                    if (node.querySelector) {
                        const author = node.querySelector(`a[href="/${target}/"]`);
                        if (author) {
                            const text = (author.textContent || '').trim().toLowerCase();
                            // Penulis asli komentar (bukan mention @) -> teks == username
                            if (text === target) {
                                blocks.add(node);
                            }
                            break;
                        }
                    }
                    node = node.parentElement;
                }
            });

            return blocks.size;
        }
        """,
        {"username": username},
    )

    return count


async def kirim_satu_komentar(page: Page, text: str) -> bool:
    """Tulis & kirim SATU komentar secara natural (per karakter).

    Logika disalin 1:1 dari ``fix copy 2.py`` (kirim_satu_komentar).
    """
    comment_selectors = [
        'textarea[placeholder="Add a comment…"]',
        'textarea[aria-label="Add a comment…"]',
        'textarea[placeholder="Tambahkan komentar…"]',
        'textarea[class*="comment"]',
        'div[contenteditable="true"]',
    ]
    comment_box = None
    for selector in comment_selectors:
        try:
            comment_box = await page.wait_for_selector(selector, timeout=3000)
            if comment_box:
                break
        except Exception:
            continue

    if not comment_box:
        try:
            icon = await page.wait_for_selector('svg[aria-label*="Comment"]', timeout=2000)
            if icon:
                await icon.click()
                await page.wait_for_timeout(1500)
                for selector in comment_selectors:
                    try:
                        comment_box = await page.wait_for_selector(selector, timeout=2000)
                        if comment_box:
                            break
                    except Exception:
                        continue
        except Exception:
            pass

    if not comment_box:
        return False

    await comment_box.click()
    await page.wait_for_timeout(random.randint(300, 700))
    for char in text:
        await page.keyboard.type(char, delay=random.randint(50, 150))
    await page.wait_for_timeout(random.randint(500, 1000))
    await page.keyboard.press("Enter")
    return True


LOGIN_ERROR_MARKERS = (
    ("password was incorrect", "Instagram password is incorrect", False),
    ("password yang anda masukkan salah", "Instagram password is incorrect", False),
    ("doesn't appear to belong", "Instagram username was not found", False),
    ("username doesn't exist", "Instagram username was not found", False),
    ("enter the code", "Instagram requires a confirmation code (2FA/challenge)", False),
    ("we sent a code", "Instagram requires a confirmation code (2FA/challenge)", False),
    ("confirm it's you", "Instagram requires identity confirmation (challenge)", False),
    ("we couldn't connect", "Instagram could not connect; retrying later", True),
    ("try again later", "Instagram rate-limited this login; retrying later", True),
    ("coba lagi nanti", "Instagram rate-limited this login; retrying later", True),
    ("please wait a few minutes", "Instagram rate-limited this login; retrying later", True),
)


async def read_session_cookie(page: Page) -> str | None:
    try:
        cookies = await page.context.cookies(INSTAGRAM_BASE_URL)
    except Exception:
        return None
    for cookie in cookies:
        if cookie.get("name") == "sessionid" and cookie.get("value"):
            return str(cookie["value"])
    return None


async def _add_session_cookie(page: Page, session_id: str) -> None:
    await page.context.add_cookies(
        [
            {
                "name": "sessionid",
                "value": session_id,
                "domain": ".instagram.com",
                "path": "/",
            }
        ]
    )


async def login_instagram(page: Page, username: str, password: str) -> None:
    logger.info("Opening Instagram login page")
    try:
        await page.goto(
            INSTAGRAM_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RetryableBotError("Instagram login page timed out") from error
    await close_popups(page)

    username_input = page.locator('input[name="email"]')
    try:
        await username_input.wait_for(state="visible", timeout=PLAYWRIGHT_TIMEOUT_MS)
    except PlaywrightTimeoutError as error:
        raise BotExecutionError("Instagram login form could not be loaded") from error

    await username_input.fill(username)
    await page.locator('input[name="pass"]').fill(password)

    # Instagram renders the real submit as an invisible input; submit via the
    # visible "Log in" button, or press Enter as a native form submit fallback.
    login_button = page.locator(
        '[role="button"]', has_text=re.compile(r"log\s*in|masuk", re.IGNORECASE)
    ).first
    try:
        if await login_button.count() and await login_button.is_visible():
            await login_button.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
        else:
            await page.locator('input[name="pass"]').press("Enter")
    except PlaywrightTimeoutError:
        await page.locator('input[name="pass"]').press("Enter")

    # Wait until we leave the login/challenge URL or an inline error appears.
    for _ in range(20):
        await page.wait_for_timeout(1500)
        current_url = page.url.lower()
        if "login" not in current_url and "challenge" not in current_url:
            break

    if "recaptcha" in current_url or "auth_platform" in current_url or "challenge" in current_url:
        raise BotExecutionError(
            "Instagram blocked automated login with a CAPTCHA/checkpoint; "
            "log in manually and provide a sessionid cookie instead"
        )

    body = ""
    try:
        body = (await page.locator("body").inner_text(timeout=PLAYWRIGHT_TIMEOUT_MS)).lower()
    except Exception:
        pass

    current_url = page.url.lower()
    for marker, message, retryable in LOGIN_ERROR_MARKERS:
        if marker in body or marker in current_url:
            if retryable:
                raise RetryableBotError(message)
            raise BotExecutionError(message)
    if "challenge" in current_url:
        raise BotExecutionError("Instagram requires additional verification (challenge/2FA)")

    # Still on the login form with password field present means failure.
    if await page.locator('input[name="pass"]').count() and "login" in current_url:
        raise BotExecutionError("Instagram login failed: unexpected error on login page")

    await page.wait_for_timeout(2500)
    await close_popups(page)
    session_cookie = await read_session_cookie(page)
    if session_cookie:
        logger.info("Instagram login successful (session cookie captured, length %s)", len(session_cookie))
    else:
        logger.info("Instagram login successful (session cookie not read)")


def decrypt_secret(encrypted_secret: str) -> str:
    encryption_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not encryption_key:
        raise BotExecutionError("Credential encryption is not configured on the worker")
    try:
        return Fernet(encryption_key.encode()).decrypt(encrypted_secret.encode()).decode()
    except (InvalidToken, ValueError) as error:
        raise BotExecutionError("Credential could not be decrypted") from error


async def _process_posts(
    page: Page,
    username: str,
    target: str,
    comment_count: int,
    log: Any | None = None,
    max_posts: int | None = None,
    tone: str = "positif",
) -> dict[str, Any]:
    """Proses postingan target persis seperti ``fix copy 2.py`` (main):

    Auto-like (hanya di postingan) -> hitung komentar @username -> top-up
    sampai ``comment_count`` memakai komentar dari DeepSeek (cadangan: bank
    lokal) sesuai ``tone`` yang dipilih user (positif/netral/negatif).
    """
    tone = normalize_tone(tone)
    commented_posts = _load_commented_state(username)
    comments_posted = 0

    if log:
        log(f"Membuka profil target @{target}")
    await open_target_profile(page, target)
    await close_popups(page)
    await check_page_access(page)

    limit = max_posts if max_posts and max_posts > 0 else int(os.getenv("MAX_POSTS_PER_JOB", "10"))
    posts = await collect_posts(page, max(1, min(limit, 50)), log=log)
    if log:
        log(f"Ditemukan {len(posts)} postingan")

    for index, post in enumerate(posts, 1):
        url = post["url"]
        caption = post.get("caption")
        saved_count = int(commented_posts.get(url, 0) or 0)
        if log:
            log(f"[{index}/{len(posts)}] Membuka {url}")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        )
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            if log:
                log("  timeout load, lanjut...")
            await page.wait_for_timeout(3000)
        await check_page_access(page)
        await close_popups(page)
        await page.wait_for_timeout(1000)

        # ===== AUTO-LIKE SEBELUM KOMENTAR (hanya di postingan) =====
        if log:
            log("  Menyukai postingan ...")
        liked = await like_if_not_liked(page)
        if log:
            log("  LIKE: " + ("OK (postingan disukai)" if liked else "gagal/tidak ditemukan"))

        # ===== HITUNG KOMENTAR =====
        existing_count = await hitung_komentar_saya(page, username)
        if saved_count > existing_count:
            existing_count = saved_count
        if log:
            log(f"  @{username} sudah {existing_count} komentar di sini (target {comment_count}).")

        if existing_count >= comment_count:
            if log:
                log("  Sudah mencapai target, lewati.")
            commented_posts[url] = existing_count
            _save_commented_state(username, commented_posts)
            continue

        need = comment_count - existing_count
        if log:
            log(f"  Perlu menambahkan {need} komentar lagi.")
        if not caption:
            if log:
                log(f"  Caption kosong, pakai komentar cadangan ({tone}).")
            comments_to_send = fallback_comments("caption tidak ditemukan", need, tone)
        else:
            if log:
                log(f"  Menyusun {need} komentar (tone: {tone})...")
            comments_to_send = await generate_ai_comments(
                caption,
                need,
                tone=tone,
                local_fallback=generate_comments_from_bank,
            )
        comments_to_send = comments_to_send[:need]
        random.shuffle(comments_to_send)

        current_count = existing_count
        for i, teks in enumerate(comments_to_send, 1):
            if log:
                log(f"  Komentar #{i}: {teks[:70]}")
            ok = await kirim_satu_komentar(page, teks)
            if ok:
                current_count += 1
                comments_posted += 1
                if log:
                    log(f"    TERKIRIM (total @{username}: {current_count})")
            else:
                if log:
                    log("    GAGAL terkirim.")
            if i < len(comments_to_send):
                await page.wait_for_timeout(random.randint(3000, 6000))

        commented_posts[url] = current_count
        _save_commented_state(username, commented_posts)
        if log:
            log(f"  Total @{username}: {current_count} komentar di postingan ini.")
        await page.wait_for_timeout(int(random.uniform(DELAY_MIN, DELAY_MAX) * 1000))

    if log:
        log(f"Selesai: {comments_posted} komentar pada {len(posts)} postingan")
    return {
        "status": "success",
        "comments_posted": comments_posted,
        "posts_processed": len(posts),
    }


async def _run_bot_async(
    username: str,
    target: str,
    comment_count: int,
    session_id: str,
    tone: str = "positif",
) -> dict[str, Any]:
    if not username.strip() or not target.strip():
        raise ValueError("username and target are required")
    if not session_id:
        raise ValueError("session_id is required")
    if comment_count < 0:
        raise ValueError("comment_count must be zero or greater")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=browser_args())
        context = await browser.new_context(
            viewport=viewport(),
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        try:
            page = await context.new_page()
            context.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
            context.set_default_navigation_timeout(PLAYWRIGHT_NAVIGATION_TIMEOUT_MS)
            await _add_session_cookie(page, session_id)
            return await _process_posts(
                page, username, target, comment_count, tone=normalize_tone(tone)
            )
        finally:
            await browser.close()


@celery_app.task(bind=True, name="run_instagram_bot")
def run_instagram_bot(
    self: Any,
    username: str,
    target: str,
    comment_count: int,
    session_id: str = "",
    tone: str = "positif",
) -> dict[str, Any]:
    tone = normalize_tone(tone)
    logger.info(
        "Starting Instagram bot task %s for %s -> %s (tone=%s)",
        self.request.id,
        username,
        target,
        tone,
    )
    self.update_state(state="PROCESSING", meta={"step": "starting_browser"})
    try:
        session = decrypt_secret(session_id) if session_id else ""
        if not session:
            raise BotExecutionError("session_id is required")
        result = asyncio.run(_run_bot_async(username, target, comment_count, session, tone))
        logger.info("Instagram bot task %s completed: %s", self.request.id, result)
        return result
    except RetryableBotError as error:
        logger.warning(
            "Retryable failure for task %s (attempt %s): %s",
            self.request.id,
            self.request.retries + 1,
            error,
        )
        try:
            raise self.retry(exc=error, countdown=300, max_retries=3)
        except MaxRetriesExceededError:
            logger.exception("Task %s exhausted retries", self.request.id)
            return {"status": "error", "message": f"Retry limit reached: {error}"}
    except Exception as error:
        logger.exception("Instagram bot task %s failed", self.request.id)
        return {"status": "error", "message": str(error)}