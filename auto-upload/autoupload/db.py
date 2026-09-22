"""Database: engine + 3 tabel (akun sosmed, post, target per platform)."""

from datetime import datetime, timezone
import logging
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from autoupload.config import DATABASE_URL

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"timeout": 30, "check_same_thread": False} if _IS_SQLITE else {},
)

if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):
        """WAL: pembaca & penulis jalan bareng -> upload browser yang lama
        tidak lagi bikin request lain kena 'database is locked'."""
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


log = logging.getLogger("autoupload")


class Base(DeclarativeBase):
    pass


class SocialAccount(Base):
    """Satu akun sosmed yang tersambung (sesi browser atau Session ID cookie)."""

    __tablename__ = "social_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Username pemilik akun ini. WAJIB dipakai sebagai saringan di setiap
    # query: Session ID setara password, jadi akun milik satu orang tidak
    # boleh terlihat - apalagi terpakai - oleh orang lain yang kebetulan
    # punya akun di website ini. Kosong berarti milik pemakaian berdiri
    # sendiri (paket ini dijalankan tanpa sistem akun website).
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    session_name: Mapped[str] = mapped_column(String(64), default="default")  # nama profil browser
    session_id: Mapped[str] = mapped_column(Text, default="")  # jalur cookie, opsional
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Post(Base):
    """Satu postingan yang disebar ke satu/beberapa platform."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Pemiliknya, dengan alasan yang sama seperti di SocialAccount: riwayat
    # memuat caption dan tautan postingan, dan itu bukan milik umum.
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    media_path: Mapped[str] = mapped_column(Text)
    media_kind: Mapped[str] = mapped_column(String(16))  # video / photo
    caption: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/processing/scheduled/done/partial/failed
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    targets: Mapped[list["PostTarget"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", lazy="selectin"
    )


class PostTarget(Base):
    """Status upload per platform untuk sebuah Post."""

    __tablename__ = "post_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("social_accounts.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(32))
    # Video/foto khusus target ini. Kosong = pakai media milik Post-nya.
    # Satu Post bisa menyebar ke banyak akun dengan video berbeda-beda.
    media_path: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")  # hasil adaptasi per platform
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/success/failed
    url: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Kemajuan 0-100 dan nama langkah yang sedang dikerjakan. Diisi oleh
    # adapter selama upload berjalan; 100 HANYA saat targetnya benar-benar
    # selesai, supaya angka penuh tidak pernah berarti "hampir".
    progress: Mapped[int] = mapped_column(Integer, default=0)
    tahap: Mapped[str] = mapped_column(String(80), default="")

    post: Mapped["Post"] = relationship(back_populates="targets")


def init_db() -> None:
    """Bikin folder data + tabel kalau belum ada. Panggil sekali saat start."""
    if _IS_SQLITE:
        # Folder data bisa saja belum ada (mis. AUTOUPLOAD_DATA_DIR diarahkan
        # ke folder project lain) - SQLite tidak membuatnya sendiri.
        db_file = Path(DATABASE_URL.split("sqlite:///", 1)[-1])
        db_file.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _tambah_kolom_yang_kurang()


def _tambah_kolom_yang_kurang() -> None:
    """Tambahkan kolom baru ke tabel yang sudah ada.

    `create_all` hanya membuat tabel yang belum ada; ia tidak pernah mengubah
    tabel lama. Tanpa ini, basis data yang sudah terisi akan gagal begitu kode
    baru memakai kolom yang belum pernah dibuat.
    """
    from sqlalchemy import inspect, text

    tambahan = {
        "post_targets": {
            "media_path": "TEXT DEFAULT ''",
            "progress": "INTEGER DEFAULT 0",
            "tahap": "VARCHAR(80) DEFAULT ''",
        },
        "social_accounts": {"owner": "VARCHAR(64) DEFAULT ''"},
        "posts": {"owner": "VARCHAR(64) DEFAULT ''"},
    }
    pemeriksa = inspect(engine)
    with engine.begin() as sambungan:
        for tabel, kolom in tambahan.items():
            if not pemeriksa.has_table(tabel):
                continue
            ada = {c["name"] for c in pemeriksa.get_columns(tabel)}
            for nama, tipe in kolom.items():
                if nama in ada:
                    continue
                sambungan.execute(text(f"ALTER TABLE {tabel} ADD COLUMN {nama} {tipe}"))
                log.info("Kolom %s.%s ditambahkan", tabel, nama)
        # Penyerahan dijalankan SETIAP kali start, bukan hanya saat kolomnya
        # baru dibuat: kalau kolom sudah terlanjur ada sebelum variabelnya
        # diisi, barisnya akan tertinggal tanpa pemilik selamanya. Idempoten -
        # yang sudah bermilik tidak disentuh.
        for tabel in ("social_accounts", "posts"):
            if pemeriksa.has_table(tabel):
                _serahkan_ke_pemilik_awal(sambungan, tabel)


def _serahkan_ke_pemilik_awal(sambungan, tabel: str) -> None:
    """Baris lama belum punya pemilik; serahkan ke pemilik yang ditunjuk.

    Tanpa ini, semua akun dan riwayat yang sudah ada mendadak tidak bermilik.
    Karena saringan pemilik menolak yang tidak cocok, akun-akun itu akan
    hilang dari halaman - bukan bocor, tapi tetap saja rusak. Nama pemiliknya
    diambil dari AUTOUPLOAD_PEMILIK_AWAL; kalau tidak diisi, barisnya sengaja
    DIBIARKAN tanpa pemilik (aman, tapi tidak terlihat) dan dicatat di log.
    """
    import os

    from sqlalchemy import text

    pemilik = os.getenv("AUTOUPLOAD_PEMILIK_AWAL", "").strip().lower()
    if not pemilik:
        log.debug(
            "%s: kolom owner baru dibuat tetapi AUTOUPLOAD_PEMILIK_AWAL kosong, "
            "jadi baris lama tidak diserahkan ke siapa pun dan tidak akan "
            "tampil di halaman. Isi variabel itu lalu jalankan ulang.",
            tabel,
        )
        return
    hasil = sambungan.execute(
        text(f"UPDATE {tabel} SET owner = :pemilik WHERE owner IS NULL OR owner = ''"),
        {"pemilik": pemilik},
    )
    if hasil.rowcount:
        log.info("%s: %s baris lama diserahkan ke %s", tabel, hasil.rowcount, pemilik)


def get_db():
    """Dependency FastAPI: satu sesi DB per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
