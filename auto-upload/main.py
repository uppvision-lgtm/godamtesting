"""Jalankan sebagai service kecil sendiri:  python main.py

Kalau website utamamu FastAPI, kamu tidak butuh file ini - cukup:
    from autoupload import router, init_db, start_scheduler
    init_db(); start_scheduler(); app.include_router(router)
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from autoupload import init_db, router, start_scheduler

STATIC = Path(__file__).parent / "autoupload" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_scheduler()
    yield


app = FastAPI(title="Auto Upload", lifespan=lifespan)
app.include_router(router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
