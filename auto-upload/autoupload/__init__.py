"""Auto-upload ke 6 platform sosmed lewat sesi browser nyata.

Dua cara pakai:

1. Langsung dari kode Python (Flask/Django/cron/CLI):

    from autoupload import init_db, upload_sync
    init_db()
    hasil = upload_sync("uploads/video.mp4", "Caption gue", ["tiktok", "twitter"])

2. Sebagai REST API (FastAPI):

    from autoupload import router, init_db, start_scheduler
    init_db(); start_scheduler()
    app.include_router(router)
"""

from autoupload.api import router
from autoupload.browser import MediaKind, PlatformError, UploadResult, session_names
from autoupload.caption import adapt_caption
from autoupload.db import Post, PostTarget, SocialAccount, init_db
from autoupload.service import create_post, dispatch_due_posts, run_post, start_scheduler
from autoupload.uploader import fan_out_upload, upload_one, upload_sync

__all__ = [
    "router",
    "init_db",
    "start_scheduler",
    "upload_sync",
    "upload_one",
    "fan_out_upload",
    "dispatch_due_posts",
    "create_post",
    "run_post",
    "adapt_caption",
    "session_names",
    "MediaKind",
    "UploadResult",
    "PlatformError",
    "Post",
    "PostTarget",
    "SocialAccount",
]
