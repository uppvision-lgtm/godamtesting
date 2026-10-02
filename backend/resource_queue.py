"""Distributed FIFO gate for memory-intensive server work."""

from __future__ import annotations

import os
import threading
import time
import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator

import redis

_QUEUE = "resource-queue:items"
_SEQUENCE = "resource-queue:sequence"
_ACTIVE = "resource-queue:active"
_ITEM = "resource-queue:item:"
_SWEEP = "resource-queue:last-sweep"
_LEASE_SECONDS = max(30, int(os.getenv("RESOURCE_QUEUE_LEASE_SECONDS", "120")))
_POLL_SECONDS = max(0.2, float(os.getenv("RESOURCE_QUEUE_POLL_SECONDS", "1")))
_STALE_TICKET_SECONDS = max(3600, int(os.getenv("RESOURCE_QUEUE_STALE_SECONDS", "86400")))
_RESERVATION_SECONDS = max(60, int(os.getenv("RESOURCE_QUEUE_RESERVATION_SECONDS", "600")))
_MAX_TICKETS = max(10, int(os.getenv("RESOURCE_QUEUE_MAX_TICKETS", "500")))
# Tiket terdepan yang tidak memegang slot dan membisu selama ini dianggap yatim.
# Penunggu yang hidup memperbarui heartbeat tiap 15 dtk dan merebut slot kosong
# dalam 1 dtk, jadi terdepan yang diam berarti pekerjaannya sudah mati (mis.
# thread Auto Outro ikut terbunuh saat backend di-deploy ulang).
_HEAD_IDLE_SECONDS = max(120, int(os.getenv("RESOURCE_QUEUE_HEAD_IDLE_SECONDS", "600")))
_client: redis.Redis | None = None


class ResourceQueueError(RuntimeError):
    """Redis is unavailable, so the server cannot safely schedule heavy work."""


class ResourceQueueCancelled(ResourceQueueError):
    """A queued operation was cancelled before receiving the server slot."""


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        try:
            _client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_timeout=5,
            )
        except Exception as error:  # noqa: BLE001
            raise ResourceQueueError("Layanan antrean server tidak tersedia.") from error
    return _client


def _hapus_tiket_stale(client: redis.Redis) -> None:
    if not client.set(_SWEEP, "1", nx=True, ex=30):
        return
    active = client.get(_ACTIVE) or ""
    sekarang = time.time()
    for ticket in client.zrange(_QUEUE, 0, -1):
        item = _ITEM + ticket
        data = client.hgetall(item)
        if not data:
            client.zrem(_QUEUE, ticket)
            continue
        try:
            terakhir = float(data.get("heartbeat") or data.get("created") or 0)
        except (TypeError, ValueError):
            terakhir = 0
        batas = _STALE_TICKET_SECONDS if data.get("claimed") == "1" else _RESERVATION_SECONDS
        if sekarang - terakhir > batas and not active.startswith(ticket + ":"):
            client.zrem(_QUEUE, ticket)
            client.delete(item)
    # Hanya tiket terdepan yang boleh jalan, jadi satu tiket yatim di depan
    # menahan semua tiket di belakangnya. Dulu ia baru dibuang setelah 24 jam.
    if active:
        return
    while True:
        pertama = client.zrange(_QUEUE, 0, 0)
        if not pertama:
            return
        ticket = pertama[0]
        try:
            terakhir = float(client.hget(_ITEM + ticket, "heartbeat") or 0)
        except (TypeError, ValueError):
            terakhir = 0
        if sekarang - terakhir <= _HEAD_IDLE_SECONDS:
            return
        client.zrem(_QUEUE, ticket)
        client.delete(_ITEM + ticket)


def buat_tiket(
    pemilik: str, label: str, jumlah: int = 1, *, reserved: bool = False
) -> str:
    """Register one FIFO request group; its place is held until all items finish."""
    if jumlah < 1:
        raise ValueError("jumlah tiket harus minimal satu")
    client = _redis()
    _hapus_tiket_stale(client)
    if client.zcard(_QUEUE) >= _MAX_TICKETS:
        raise ResourceQueueError("Antrean server penuh. Coba lagi setelah sebagian pekerjaan selesai.")
    ticket = uuid.uuid4().hex
    sequence = client.incr(_SEQUENCE)
    pipe = client.pipeline()
    pipe.zadd(_QUEUE, {ticket: sequence})
    pipe.hset(
        _ITEM + ticket,
        mapping={
            "owner": str(pemilik or "tamu").strip().lower(),
            "label": str(label or "Pekerjaan server"),
            "remaining": jumlah,
            "created": time.time(),
            "heartbeat": time.time(),
            "claimed": 0 if reserved else 1,
        },
    )
    pipe.execute()
    return ticket


def _renew(ticket: str, run_id: str) -> bool:
    result = _redis().eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "redis.call('expire', KEYS[1], ARGV[2]); "
        "redis.call('hset', KEYS[2], 'heartbeat', ARGV[3]); return 1 "
        "else return 0 end",
        2,
        _ACTIVE,
        _ITEM + ticket,
        f"{ticket}:{run_id}",
        _LEASE_SECONDS,
        time.time(),
    )
    return bool(result)


def ambil_giliran(
    ticket: str,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Block until this batch is first in line and owns the single server slot."""
    client = _redis()
    run_id = uuid.uuid4().hex
    lock_value = f"{ticket}:{run_id}"
    mulai = time.monotonic()
    heartbeat_berikut = mulai
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise ResourceQueueCancelled("Pekerjaan antrean dibatalkan.")
        item = _ITEM + ticket
        if not client.exists(item):
            raise ResourceQueueError("Tiket antrean sudah kedaluwarsa.")
        if client.hget(item, "claimed") != "1":
            raise ResourceQueueError("Tiket antrean belum dipakai oleh pemiliknya.")
        pertama = client.zrange(_QUEUE, 0, 0)
        if pertama and pertama[0] == ticket and client.set(
            _ACTIVE, lock_value, nx=True, ex=_LEASE_SECONDS
        ):
            client.hset(_ITEM + ticket, "heartbeat", time.time())
            return run_id
        if time.monotonic() >= heartbeat_berikut:
            client.hset(_ITEM + ticket, "heartbeat", time.time())
            heartbeat_berikut = time.monotonic() + 15
        if timeout is not None and time.monotonic() - mulai >= timeout:
            raise TimeoutError("Menunggu giliran antrean server melewati batas waktu.")
        time.sleep(_POLL_SECONDS)


def selesaikan_item(ticket: str, run_id: str) -> None:
    """Release the active slot and remove the batch ticket after its final item."""
    script = """
    if redis.call('get', KEYS[1]) ~= ARGV[1] then return -1 end
    redis.call('del', KEYS[1])
    if redis.call('exists', KEYS[3]) == 0 then return 0 end
    local remaining = redis.call('hincrby', KEYS[3], 'remaining', -1)
    if remaining <= 0 then
      redis.call('zrem', KEYS[2], ARGV[2])
      redis.call('del', KEYS[3])
    end
    return remaining
    """
    _redis().eval(
        script,
        3,
        _ACTIVE,
        _QUEUE,
        _ITEM + ticket,
        f"{ticket}:{run_id}",
        ticket,
    )


@contextmanager
def giliran(
    ticket: str, cancel_event: threading.Event | None = None
) -> Iterator[None]:
    """Hold the global slot for one batch item, renewing its lease as needed."""
    run_id = ambil_giliran(ticket, cancel_event=cancel_event)
    berhenti = threading.Event()

    def jaga_lease() -> None:
        while not berhenti.wait(max(5, _LEASE_SECONDS // 3)):
            try:
                if not _renew(ticket, run_id):
                    return
            except redis.RedisError:
                continue

    penjaga = threading.Thread(target=jaga_lease, name="resource-queue-lease", daemon=True)
    penjaga.start()
    try:
        yield
    finally:
        berhenti.set()
        penjaga.join(timeout=2)
        selesaikan_item(ticket, run_id)

@asynccontextmanager
async def giliran_async(
    ticket: str, cancel_event: threading.Event | None = None
) -> Iterator[None]:
    """Async variant that does not block an application's event loop while queued."""
    run_id = await asyncio.to_thread(ambil_giliran, ticket, None, cancel_event)
    berhenti = threading.Event()

    def jaga_lease() -> None:
        while not berhenti.wait(max(5, _LEASE_SECONDS // 3)):
            try:
                if not _renew(ticket, run_id):
                    return
            except redis.RedisError:
                continue

    penjaga = threading.Thread(target=jaga_lease, name="resource-queue-lease", daemon=True)
    penjaga.start()
    try:
        yield
    finally:
        berhenti.set()
        penjaga.join(timeout=2)
        await asyncio.to_thread(selesaikan_item, ticket, run_id)


def kurangi_tiket(ticket: str, pemilik: str, jumlah: int = 1) -> bool:
    """Drop unsubmitted items from a batch reservation."""
    script = """
        if redis.call('hget', KEYS[2], 'owner') ~= ARGV[2] then return 0 end
        local remaining = redis.call('hincrby', KEYS[2], 'remaining', -tonumber(ARGV[3]))
    if remaining <= 0 then
      if string.sub(redis.call('get', KEYS[3]) or '', 1, string.len(ARGV[1])) ~= ARGV[1] then
        redis.call('zrem', KEYS[1], ARGV[1])
        redis.call('del', KEYS[2])
      else
        redis.call('hset', KEYS[2], 'remaining', 0)
      end
    end
    return 1
    """
    return bool(
        _redis().eval(
            script,
            3,
            _QUEUE,
            _ITEM + ticket,
            _ACTIVE,
            ticket,
            str(pemilik or "tamu").strip().lower(),
            jumlah,
        )
    )


def status_antrean(pemilik: str) -> dict[str, Any]:
    """Return global activity and positions for a user's queued batches."""
    client = _redis()
    _hapus_tiket_stale(client)
    ids = client.zrange(_QUEUE, 0, -1)
    active = client.get(_ACTIVE)
    posisi = []
    aktif: dict[str, str] | None = None
    for index, ticket in enumerate(ids):
        data = client.hgetall(_ITEM + ticket)
        if not data:
            continue
        state = "running" if active and active.startswith(ticket + ":") else "queued"
        if state == "running":
            aktif = {"label": data.get("label", "Pekerjaan server"), "ticket_id": ticket}
        if data.get("owner", "") == str(pemilik or "tamu").strip().lower():
            posisi.append(
                {
                    "ticket_id": ticket,
                    "label": data.get("label", "Pekerjaan server"),
                    "position": index + 1,
                    "status": state,
                    "remaining": int(data.get("remaining", "1")),
                }
            )
    return {"active": aktif, "waiting": len(ids), "tickets": posisi}


def tiket_milik(ticket: str, pemilik: str) -> bool:
    script = """
    if redis.call('hget', KEYS[1], 'owner') ~= ARGV[1] then return 0 end
    redis.call('hset', KEYS[1], 'claimed', 1, 'heartbeat', ARGV[2])
    return 1
    """
    return bool(
        _redis().eval(
            script,
            1,
            _ITEM + ticket,
            str(pemilik or "tamu").strip().lower(),
            time.time(),
        )
    )


def batalkan_tiket(ticket: str, pemilik: str) -> bool:
    """Remove a ticket only if it is queued and owned by the caller."""
    script = """
    if redis.call('hget', KEYS[2], 'owner') ~= ARGV[1] then return 0 end
    if string.sub(redis.call('get', KEYS[3]) or '', 1, string.len(ARGV[2])) == ARGV[2] then return 0 end
    redis.call('zrem', KEYS[1], ARGV[2])
    redis.call('del', KEYS[2])
    return 1
    """
    return bool(
        _redis().eval(
            script,
            3,
            _QUEUE,
            _ITEM + ticket,
            _ACTIVE,
            str(pemilik or "tamu").strip().lower(),
            ticket,
        )
    )