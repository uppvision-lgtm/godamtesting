"""Adapter upload per platform (semua lewat sesi browser Playwright)."""

from autoupload.platforms.facebook import Facebook
from autoupload.platforms.instagram import Instagram
from autoupload.platforms.threads import Threads
from autoupload.platforms.tiktok import TikTok
from autoupload.platforms.twitter import Twitter
from autoupload.platforms.youtube import YouTube

REGISTRY = {
    "tiktok": TikTok,
    "instagram": Instagram,
    "facebook": Facebook,
    "youtube": YouTube,
    "threads": Threads,
    "twitter": Twitter,
}

__all__ = ["REGISTRY", "TikTok", "Instagram", "Facebook", "YouTube", "Threads", "Twitter"]
