"""Transactional transfer metadata; private payloads; one service process."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from pathlib import Path
from boring_service_kit.outcomes import Refused, Failed

MAX_FILE_BYTES = 2 * 1024**3
QUOTA_BYTES = 10 * 1024**3
LIFETIME_SECONDS = 86400
RETRY_SECONDS = 600
# Slowest transfer rate the fixed ten-minute window is stretched for: a large
# file gets size / MIN_TRANSFER_RATE seconds (within the transfer's expiry).
MIN_TRANSFER_RATE = 1024 * 1024  # bytes per second


def transfer_window(size):
    """Seconds one upload or claimed download may take for a file of this size."""
    return max(RETRY_SECONDS, (size or 0) / MIN_TRANSFER_RATE)
LIST_LIMIT = 100
CHUNK = 256 * 1024
ID = re.compile(r"[a-f0-9]{32}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
TERMINAL = {"deleted", "expired", "collected"}
LIVE = {"pending", "uploading", "ready", "claimed"}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def flush_directory(directory: Path) -> bool:
    """Make the entries of `directory` durable: a name just linked or renamed
    into it survives a crash once this returns True. A durable file (fsync)
    does not make its new name durable on every POSIX filesystem; fsync of the
    directory does.

    Raises OSError when the flush fails: the caller must not act as if the
    entry were durable. Returns False on Windows, where Python cannot open a
    directory to flush it: NTFS journals the link, but no flush is made here,
    so no crash guarantee is claimed (the caller reports that)."""
    if os.name == "nt":
        return False
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 240
        or value in {".", ".."}
        or any(c in value for c in "/\\")
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise Refused(
            "invalid_request",
            "filename must be a plain display name, at most 240 UTF-8 bytes",
        )
    return value


class Store:
    def __init__(
        self,
        root: Path,
        *,
        max_file=MAX_FILE_BYTES,
        quota=QUOTA_BYTES,
        lifetime=LIFETIME_SECONDS,
        retry=RETRY_SECONDS,
        clock=time.time,
    ):
        if any(
            type(v) is not int or v <= 0 for v in (max_file, quota, lifetime, retry)
        ):
            raise ValueError("limits must be positive integers")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and self.root.stat().st_mode & 0o077:
            raise ValueError("state directory must be private (0700)")
        with ExitStack() as resources:
            self.guard = (self.root / "process.lock").open("a+b")
            resources.callback(self.guard.close)
            # This implementation owns one process, not an unsafe second SQLite writer.
            try:
                if os.name == "nt":
                    import msvcrt

                    self.guard.seek(0)
                    self.guard.write(b"0")
                    self.guard.flush()
                    self.guard.seek(0)
                    msvcrt.locking(self.guard.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("state directory already in use") from None
            self.payloads = self.root / "payloads"
            self.payloads.mkdir(exist_ok=True, mode=0o700)
            self.max_file, self.quota = max_file, quota
            self.lifetime, self.retry, self.clock = lifetime, retry, clock
            self.lock = threading.RLock()
            self.active: dict[str, int] = {}
            self.db = sqlite3.connect(
                self.root / "transfers.sqlite", check_same_thread=False
            )
            resources.callback(self.db.close)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS transfers (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, direction TEXT NOT NULL,
              name TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
              expires REAL NOT NULL, capacity INTEGER NOT NULL, reserved INTEGER NOT NULL,
              size INTEGER, sha TEXT, expected_size INTEGER, expected_sha TEXT,
              upload_hash TEXT, download_hash TEXT, browser_hash TEXT, claim_until REAL
            );
            CREATE TABLE IF NOT EXISTS grants (
              transfer_id TEXT NOT NULL, collector TEXT NOT NULL,
              PRIMARY KEY(transfer_id,collector)
            );
            """)
            self.reconcile()

            self.resources = resources.pop_all()

    def close(self):
        self.resources.close()

    def path(self, transfer_id, suffix=".bin"):
        if not isinstance(transfer_id, str) or not ID.fullmatch(transfer_id):
            raise Refused("not_found", "transfer not found")
        return self.payloads / (transfer_id + suffix)

    def _row(self, transfer_id):
        self.path(transfer_id)
        row = self.db.execute(
            "SELECT * FROM transfers WHERE id=?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise Refused("not_found", "transfer not found")
        return dict(row)

    def _live(self, row, now=None):
        deadline = row["claim_until"] if row["state"] == "claimed" else row["expires"]
        return row["state"] in LIVE and deadline > (
            self.clock() if now is None else now
        )

    def _can_access(self, row, caller, owner_only=False):
        if row["owner"] == caller:
            return True
        return (
            not owner_only
            and row["direction"] == "upload"
            and bool(
                self.db.execute(
                    "SELECT 1 FROM grants WHERE transfer_id=? AND collector=?",
                    (row["id"], caller),
                ).fetchone()
            )
        )

    def _allowed(self, row, caller, owner_only=False):
        if not self._can_access(row, caller, owner_only):
            raise Refused("not_found", "transfer not found")

    @staticmethod
    def _receipt(row):
        return {
            k: row[k]
            for k in (
                "id",
                "direction",
                "name",
                "state",
                "created",
                "expires",
                "size",
                "sha",
                "claim_until",
            )
        }

    def status(self, transfer_id, caller):
        with self.lock:
            row = self._row(transfer_id)
            self._allowed(row, caller)
            if not self._live(row) and row["state"] in LIVE:
                raise Refused("not_found", "transfer expired")
            return self._receipt(row)

    def _receipts(self, rows, caller):
        # One time snapshot; filter before the bound, so expired rows cannot hide live work.
        now = self.clock()
        result = []
        for row in rows:
            if (row["state"] not in LIVE or self._live(row, now)) and self._can_access(
                row, caller
            ):
                result.append(self._receipt(row))
                if len(result) == LIST_LIMIT:
                    break
        return {"transfers": result, "limit": LIST_LIMIT}

    def list_transfers(self, caller):
        """Owner recovery: live work first, then retained terminal receipts."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM transfers WHERE owner=? ORDER BY created,id", (caller,)
            ).fetchall()
            rows.sort(key=lambda row: row["state"] in TERMINAL)
            return self._receipts(rows, caller)

    def list_ready(self, caller):
        with self.lock:
            rows = self.db.execute("""SELECT * FROM transfers
                WHERE direction='upload' AND state='ready' ORDER BY created,id""").fetchall()
            return self._receipts(rows, caller)

    def _collection_row(self, transfer_id, caller):
        row = self._row(transfer_id)
        self._allowed(row, caller)
        if (
            row["direction"] != "upload"
            or row["state"] != "ready"
            or not self._live(row)
        ):
            raise Refused("not_found", "upload not ready")
        return row

    def collection_receipt(self, transfer_id, caller):
        with self.lock:
            return self._receipt(self._collection_row(transfer_id, caller))

    def create(self, owner, direction, name="", size=None, sha=None, max_bytes=None):
        if direction not in {"upload", "download"}:
            raise Refused("invalid_request", "direction must be upload or download")
        if direction == "download":
            if max_bytes is not None:
                raise Refused(
                    "invalid_request", "deposit uses exact size, not max_bytes"
                )
            name = filename(name)
            if (
                type(size) is not int
                or size < 0
                or not isinstance(sha, str)
                or not SHA.fullmatch(sha)
            ):
                raise Refused("invalid_request", "deposit needs exact size and SHA256")
            capacity = max(1, size)
        else:
            if name:
                name = filename(name)
            if size is not None or sha is not None:
                raise Refused(
                    "invalid_request",
                    "browser upload uses max_bytes, not a preset digest",
                )
            capacity = self.max_file if max_bytes is None else max_bytes
        if type(capacity) is not int or not 1 <= capacity <= self.max_file:
            raise Refused("limit", "file size exceeds configured limit")
        self.cleanup()  # before the lock: it unlinks outside it (see cleanup)
        with self.lock:
            if self.db.execute("SELECT count(*) FROM transfers").fetchone()[0] >= 4096:
                raise Refused("limit", "transfer metadata capacity reached")
            used = self.db.execute(
                "SELECT coalesce(sum(reserved),0) FROM transfers"
            ).fetchone()[0]
            if used + capacity > self.quota:
                raise Refused("limit", "temporary storage quota reached")
            transfer_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            now = self.clock()
            with self.db:
                self.db.execute(
                    """INSERT INTO transfers
                (id,owner,direction,name,state,created,expires,capacity,reserved,
                 expected_size,expected_sha,upload_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        transfer_id,
                        owner,
                        direction,
                        name,
                        "pending",
                        now,
                        now + self.lifetime,
                        capacity,
                        capacity,
                        size,
                        sha,
                        fingerprint(token),
                    ),
                )
            return {
                "id": transfer_id,
                "upload_token": token,
                "expires": now + self.lifetime,
                "max_bytes": capacity,
                "direction": direction,
            }

    def upload_start(self, transfer_id, token, name, declared):
        name = filename(name)
        with self.lock:
            row = self._row(transfer_id)
            if (
                not self._live(row)
                or not token
                or row["upload_hash"] != fingerprint(token)
            ):
                raise Refused("forbidden", "upload capability invalid or expired")
            if row["state"] != "pending":
                raise Refused("busy", "upload unavailable")
            if type(declared) is not int or not 0 <= declared <= row["capacity"]:
                raise Refused("limit", "upload length exceeds reservation")
            if row["expected_size"] is not None and declared != row["expected_size"]:
                raise Refused("invalid_request", "deposit size changed")
            if row["direction"] == "download" and name != row["name"]:
                raise Refused("invalid_request", "deposit filename changed")
            with self.db:
                self.db.execute(
                    "UPDATE transfers SET state='uploading',name=? WHERE id=?",
                    (name, transfer_id),
                )
            self.active[transfer_id] = self.active.get(transfer_id, 0) + 1
            return row

    def upload_finish(self, transfer_id, size, sha):
        with self.lock:
            row = self._row(transfer_id)
            if not self._live(row) or row["state"] != "uploading":
                raise Failed("expired", "upload expired before publication")
            if row["expected_size"] is not None and (
                size != row["expected_size"] or sha != row["expected_sha"]
            ):
                raise Failed(
                    "checksum",
                    "deposit did not match declared size/checksum; no file published",
                )
            os.replace(self.path(transfer_id, ".part"), self.path(transfer_id))
            # The payload's data was fsynced before the rename (upload_io);
            # its new name must be durable before 'ready' says it exists.
            try:
                flush_directory(self.payloads)
            except OSError:
                raise Failed("storage", "upload could not be made durable; no publication") from None
            with self.db:
                self.db.execute(
                    """UPDATE transfers SET state='ready',size=?,sha=?,
                                reserved=?,upload_hash=NULL WHERE id=?""",
                    (size, sha, size, transfer_id),
                )

    def _discard_unpublished(self, row):
        transfer_id = row["id"]
        self.path(transfer_id, ".part").unlink(missing_ok=True)
        if row["state"] in {"pending", "uploading"}:
            # Also covers failure after rename but before metadata publication.
            self.path(transfer_id).unlink(missing_ok=True)
        if row["state"] == "uploading":
            with self.db:
                self.db.execute(
                    "UPDATE transfers SET state='pending' WHERE id=?", (transfer_id,)
                )

    def published(self, transfer_id):
        """(size, sha) if this upload was published, else None."""
        with self.lock:
            row = self._row(transfer_id)
            return (row["size"], row["sha"]) if row["state"] == "ready" else None

    def upload_abort(self, transfer_id):
        with self.lock:
            self._discard_unpublished(self._row(transfer_id))

    def release(self, transfer_id):
        with self.lock:
            self.active[transfer_id] -= 1
            if self.active[transfer_id] == 0:
                del self.active[transfer_id]
        self.cleanup()

    def open_download(self, transfer_id, owner):
        with self.lock:
            row = self._row(transfer_id)
            self._allowed(row, owner, True)
            if (
                row["direction"] != "download"
                or row["state"] != "ready"
                or not self._live(row)
            ):
                raise Refused("not_found", "download not ready")
            token = secrets.token_urlsafe(32)
            with self.db:
                self.db.execute(
                    "UPDATE transfers SET download_hash=? WHERE id=?",
                    (fingerprint(token), transfer_id),
                )
            return {**self._receipt(row), "token": token}

    def claim(self, transfer_id, token, browser):
        with self.lock:
            row = self._row(transfer_id)
            if (
                row["direction"] != "download"
                or not self._live(row)
                or not token
                or row["download_hash"] != fingerprint(token)
            ):
                raise Refused("forbidden", "download capability invalid or expired")
            if row["state"] == "claimed":
                if row["browser_hash"] != fingerprint(browser):
                    raise Refused("forbidden", "download already claimed")
                return row["claim_until"]
            if row["state"] != "ready":
                raise Refused("not_found", "download not ready")
            # A configured retry window is used as given; the default one grows
            # with the file so a slow link can finish a large download.
            window = transfer_window(row["size"]) if self.retry == RETRY_SECONDS else self.retry
            until = self.clock() + window
            with self.db:
                self.db.execute(
                    "UPDATE transfers SET state='claimed',browser_hash=?,claim_until=? WHERE id=?",
                    (fingerprint(browser), until, transfer_id),
                )
            return until

    def _reader_row(self, transfer_id, browser, caller):
        if caller is not None:
            return self._collection_row(transfer_id, caller)
        row = self._row(transfer_id)
        if not self._live(row):
            raise Refused("not_found", "file expired")
        if (
            row["direction"] != "download"
            or row["state"] != "claimed"
            or not browser
            or row["browser_hash"] != fingerprint(browser)
        ):
            raise Refused("forbidden", "browser session not valid")
        return row

    @contextmanager
    def reader(self, transfer_id, *, browser=None, caller=None):
        with self.lock:
            row = self._reader_row(transfer_id, browser, caller)
            handle = self.path(transfer_id).open("rb")
            self.active[transfer_id] = self.active.get(transfer_id, 0) + 1
        try:
            yield row, handle
        finally:
            handle.close()
            self.release(transfer_id)

    def valid_stream(self, transfer_id, *, browser=None, caller=None):
        with self.lock:
            if browser is None and caller is None:
                return self._live(self._row(transfer_id))
            try:
                self._reader_row(transfer_id, browser, caller)
                return True
            except Refused:
                return False

    def ack(self, transfer_id, caller, size, sha):
        with self.lock:
            row = self._row(transfer_id)
            self._allowed(row, caller)
            if row["direction"] != "upload" or size != row["size"] or sha != row["sha"]:
                raise Refused("invalid_request", "receipt does not match upload")
            if row["state"] == "collected":
                return {"collected": True}
            if row["state"] != "ready" or not self._live(row):
                raise Refused("not_found", "upload not ready")
            self._terminal(transfer_id, "collected")
        self.cleanup()
        return {"collected": True}

    def grant(self, transfer_id, owner, collector, enabled=True):
        if not isinstance(collector, str) or not 0 < len(collector) <= 512:
            raise Refused(
                "invalid_request",
                "collector must be a verified service author identifier",
            )
        with self.lock:
            row = self._row(transfer_id)
            self._allowed(row, owner, True)
            if row["direction"] != "upload" or not self._live(row):
                raise Refused("not_found", "upload not available")
            with self.db:
                if enabled:
                    self.db.execute(
                        "INSERT OR IGNORE INTO grants VALUES (?,?)",
                        (transfer_id, collector),
                    )
                else:
                    self.db.execute(
                        "DELETE FROM grants WHERE transfer_id=? AND collector=?",
                        (transfer_id, collector),
                    )
            return {"granted": enabled}

    def revoke(self, transfer_id, owner):
        with self.lock:
            row = self._row(transfer_id)
            self._allowed(row, owner, True)
            if row["state"] not in TERMINAL:
                self._terminal(transfer_id, "deleted")
        self.cleanup()
        return {"revoked": True}

    def _terminal(self, transfer_id, state):
        with self.db:
            self.db.execute(
                """UPDATE transfers SET state=?,upload_hash=NULL,
            download_hash=NULL,browser_hash=NULL WHERE id=?""",
                (state, transfer_id),
            )
            # Collector must retain authority to retry a completed acknowledgement.

    def cleanup(self):
        """Expire what is past its deadline, delete the payloads of terminal
        transfers nobody reads or writes, then release their quota.

        The unlinks (slow for a large payload) run outside the store lock.
        That is safe because terminal is final: no method moves a transfer out
        of TERMINAL, and only live transfers become active (upload_start,
        reader), so a payload collected here can no longer be opened by
        anyone. Quota is released only after its payload is gone, as before:
        an unlink that fails keeps the reservation and stops here; the next
        cleanup retries it (unlinks are idempotent, concurrent cleanups too).
        Callers holding the lock still unlink under it, so the store's own
        methods call cleanup after leaving theirs."""
        with self.lock:
            now = self.clock()
            settled = []
            for record in self.db.execute("SELECT * FROM transfers").fetchall():
                row = dict(record)
                if row["state"] in LIVE and not self._live(row):
                    self._terminal(row["id"], "expired")
                    row["state"] = "expired"
                if row["state"] in TERMINAL and row["id"] not in self.active:
                    settled.append((row["id"], now > row["expires"] + 86400))
        for transfer_id, forget in settled:
            self.path(transfer_id).unlink(missing_ok=True)
            self.path(transfer_id, ".part").unlink(missing_ok=True)
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE transfers SET reserved=0 WHERE id=?", (transfer_id,)
                )
                if forget:
                    self.db.execute(
                        "DELETE FROM grants WHERE transfer_id=?", (transfer_id,)
                    )
                    self.db.execute(
                        "DELETE FROM transfers WHERE id=?", (transfer_id,)
                    )

    def reconcile(self):
        with self.lock:
            # Only at startup, under the process lock: no running reader/writer exists.
            self.cleanup()
            for record in self.db.execute("SELECT * FROM transfers").fetchall():
                row = dict(record)
                self._discard_unpublished(row)
                if (
                    row["state"] in {"ready", "claimed"}
                    and not self.path(row["id"]).is_file()
                ):
                    raise RuntimeError(
                        "published payload missing; state needs operator inspection"
                    )
            known = {r[0] for r in self.db.execute("SELECT id FROM transfers")}
            for p in self.payloads.iterdir():
                if (
                    p.name.endswith((".bin", ".part"))
                    and ID.fullmatch(p.stem)
                    and p.stem not in known
                ):
                    p.unlink()
            self.cleanup()
