"""Database: engine + 3 tabel (akun sosmed, post, target per platform)."""

from datetime import datetime, timezone
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


class Base(DeclarativeBase):
    pass


class SocialAccount(Base):
    """Satu akun sosmed yang tersambung (sesi browser atau Session ID cookie)."""

    __tablename__ = "social_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
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
    caption: Mapped[str] = mapped_column(Text, default="")  # hasil adaptasi per platform
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/success/failed
    url: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    post: Mapped["Post"] = relationship(back_populates="targets")


def init_db() -> None:
    """Bikin folder data + tabel kalau belum ada. Panggil sekali saat start."""
    if _IS_SQLITE:
        # Folder data bisa saja belum ada (mis. AUTOUPLOAD_DATA_DIR diarahkan
        # ke folder project lain) - SQLite tidak membuatnya sendiri.
        db_file = Path(DATABASE_URL.split("sqlite:///", 1)[-1])
        db_file.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    """Dependency FastAPI: satu sesi DB per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
