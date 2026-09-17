"""Adaptasi caption ke batas & gaya tiap platform. Fungsi murni, tanpa network."""

import re

# limit karakter + budget hashtag per platform
RULES: dict[str, dict] = {
    "tiktok": {"limit": 2200, "max_hashtags": 5},
    "instagram": {"limit": 2200, "max_hashtags": 10},
    "facebook": {"limit": 5000, "max_hashtags": 3},
    "youtube": {"limit": 5000, "max_hashtags": 3},
    "threads": {"limit": 500, "max_hashtags": 2},
    "twitter": {"limit": 280, "max_hashtags": 2},
}
DEFAULT_RULE = {"limit": 2200, "max_hashtags": 3}


def _compress(text: str, max_chars: int) -> str:
    """Potong di batas kalimat/kata terdekat, jangan di tengah kata."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in (". ", "! ", "? ", " "):
        idx = truncated.rfind(sep)
        if idx > max_chars * 0.5:
            return truncated[: idx + 1].strip()
    return truncated.strip()


def adapt_caption(platform: str, caption: str) -> str:
    """Sesuaikan satu caption master untuk platform tertentu.

    Tanpa ini caption panjang bikin upload X/Threads gagal (limit 280/500).
    """
    rules = RULES.get(platform, DEFAULT_RULE)
    limit, max_tags = rules["limit"], rules["max_hashtags"]

    tags = re.findall(r"#\w+", caption)
    body = re.sub(r"#\w+", "", caption).strip()

    if platform in ("twitter", "threads"):
        reserved = len(" ".join(tags[:max_tags])) + 1 if tags else 0
        body = _compress(body, max(80, limit - reserved))

    kept = " ".join(tags[:max_tags])
    result = f"{body}\n\n{kept}".strip() if kept else body
    if len(result) > limit and kept:
        result = body                      # hashtag-nya yang bikin lewat limit
    if len(result) > limit:
        result = result[: limit - 1].rstrip() + "…"
    return result
