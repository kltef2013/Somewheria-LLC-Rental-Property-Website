"""SQLite-backed storage service.

Mirrors the public API of ``FileStorageService`` so it can be swapped in via
the ``USE_SQLITE_STORAGE`` feature flag without touching any callers. The
flag is OFF by default and runtime behavior is unchanged unless explicitly
opted in.

Only the methods actually used elsewhere in the app are reimplemented here.
``load_json_file`` / ``save_json_file`` are also provided so that
``TicketService`` (which calls them directly with ``config.tickets_file``)
keeps working when handed a ``SqlStorageService``. The path argument is used
as a routing key — ``config.tickets_file`` routes to the ``tickets`` table.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

from .console import get_console_logger
from .database import Database, dumps, loads


class SqlStorageService:
    def __init__(self, config) -> None:
        self.config = config
        self.logger = get_console_logger("storage-sql")
        self.db = Database(config.sqlite_file)
        # Re-entrant lock backing :meth:`atomic`. Individual writes already
        # go through ``db.transaction()``, but a route-level
        # read-modify-write sequence spans multiple transactions — two
        # concurrent admins both load the pre-write snapshot and race each
        # other's saves, silently dropping one of the updates. Holding this
        # lock around the route block (via ``with storage.atomic():``)
        # serializes those sequences in this process, mirroring the
        # guarantee :class:`FileStorageService` gets from ``file_lock``.
        # Per-process is sufficient for the single-worker deployment model
        # documented in ``services/storage.py``.
        self._atomic_lock = threading.RLock()

    @contextlib.contextmanager
    def atomic(self):
        """Serialize a multi-step read-modify-write across writers.

        SQL writes already go through ``db.transaction()``, but a load+save
        sequence in a route handler still races concurrent callers because
        each call opens its own transaction. Holding ``self._atomic_lock``
        across the block makes the whole sequence a single critical
        section, exactly like ``FileStorageService.atomic()`` does on the
        file backend.
        """
        with self._atomic_lock:
            yield

    # ---------------------------------------------------------------- helpers

    def _safe_loads(self, text, *, source: str):
        """Deserialize a JSON column value, returning ``None`` on decode failure.

        The write paths only ever store valid JSON, so in normal operation this
        is a no-op — but a hand-edited row / a partial INSERT / on-disk
        corruption can leave malformed JSON in a ``payload`` / ``profile`` /
        ``contract`` column. A bare ``json.loads`` would raise
        ``JSONDecodeError`` up through the caller and take out the admin
        dashboard, renter dashboard, or ticket routes via the crash handler's
        empty 503. Callers pair this with the existing ``isinstance(dict)``
        filter so a ``None`` return simply drops the row, matching the
        already-shipped defensive shape for non-dict-but-valid JSON.
        Mirrors the ``json.loads`` try/except ``analytics.recent_listing_activity``
        already applies to the JSONL change log, and closes the same gap on
        the file backend's ``load_json_file`` (which wraps its own decode in
        a try/except and returns the caller's default on failure).
        """
        try:
            return loads(text)
        except (ValueError, TypeError) as exc:
            self.logger.warning("Skipping malformed JSON row in %s: %s", source, exc)
            return None

    # (config attribute name, routing key). ``getattr`` with a ``None``
    # default means a config missing one of these attributes reports an
    # unknown path (falls back to the caller's default / no-op) rather
    # than raising AttributeError halfway through the check — matching
    # the contract exercised by ``PathShimUnknownPathTestCase``.
    _PATH_KEY_ATTRS = (
        ("registration_file", "pending_registrations"),
        ("user_roles_file", "user_roles"),
        ("renter_profile_file", "renter_profiles"),
        ("contracts_file", "renter_contracts"),
        ("tickets_file", "tickets"),
        ("lead_capture_file", "lead_captures"),
        ("hidden_listings_file", "hidden_listings"),
    )

    def _path_key(self, path: Path) -> str:
        path = Path(path)
        for attr, key in self._PATH_KEY_ATTRS:
            configured = getattr(self.config, attr, None)
            if configured is not None and path == configured:
                return key
        return ""

    # ---------------- Generic JSON shim used by TicketService et al --------

    def load_json_file(self, path, default, *, expected_type=None):
        key = self._path_key(path)
        if not key:
            self.logger.warning("Unknown storage path %s; returning default", path)
            return default
        try:
            if key == "tickets":
                return self._load_tickets()
            if key == "user_roles":
                return self.get_user_roles()
            if key == "pending_registrations":
                return self.get_pending_registrations()
            if key == "renter_profiles":
                return self.get_renter_profiles()
            if key == "renter_contracts":
                return self.get_renter_contracts()
            if key == "lead_captures":
                return self.get_pending_lead_captures()
            if key == "hidden_listings":
                return self.get_hidden_listing_ids()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error("Failed to load %s from sqlite: %s", path, exc)
        return default

    def save_json_file(self, path, data) -> None:
        key = self._path_key(path)
        if not key:
            self.logger.warning("Unknown storage path %s; ignoring write", path)
            return
        try:
            if key == "tickets":
                self._save_tickets(data)
            elif key == "user_roles":
                self._replace_user_roles(data)
            elif key == "pending_registrations":
                self._replace_pending_registrations(data)
            elif key == "renter_profiles":
                self.save_renter_profiles(data)
            elif key == "renter_contracts":
                self.save_renter_contracts(data)
            elif key == "lead_captures":
                self._replace_pending_lead_captures(data)
            elif key == "hidden_listings":
                self._replace_hidden_listings(data)
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error("Failed to save %s to sqlite: %s", path, exc)

    # ----------------------------------------------------- pending_registrations

    def get_pending_registrations(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT payload FROM pending_registrations").fetchall()
        # Filter out any row whose stored ``payload`` doesn't deserialize to
        # a dict. ``add_pending_registration`` / ``_replace_pending_registrations``
        # only ever write dicts, so in normal operation this is a no-op — but a
        # hand-edited row could hold anything, and every downstream caller
        # (dedup, admin_registrations render, remove) calls ``.get("email")``
        # on each item, which would AttributeError and 503 the admin UI via
        # the crash handler. Matches the FileStorageService guard added
        # alongside this change. ``_safe_loads`` also swallows malformed JSON
        # so an on-disk-corrupted row can't take the caller down with a
        # ``JSONDecodeError`` before the dict filter even runs.
        return [
            payload
            for payload in (self._safe_loads(row["payload"], source="pending_registrations") for row in rows)
            if isinstance(payload, dict)
        ]

    def add_pending_registration(self, registration: dict) -> bool:
        email = (registration.get("email") or "").strip().lower()
        if not email:
            return False
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM pending_registrations WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO pending_registrations(email, payload) VALUES (?, ?)",
                (email, dumps(registration)),
            )
        return True

    def remove_pending_registration(self, email: str) -> None:
        email = (email or "").strip().lower()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM pending_registrations WHERE email = ?", (email,))

    def _replace_pending_registrations(self, data: list[dict]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM pending_registrations")
            for item in data or []:
                email = (item.get("email") or "").strip().lower()
                if not email:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO pending_registrations(email, payload) VALUES (?, ?)",
                    (email, dumps(item)),
                )

    # ------------------------------------------------------------- user_roles

    def get_user_roles(self) -> dict:
        with self.db.read() as conn:
            rows = conn.execute("SELECT email, role FROM user_roles").fetchall()
        # Drop rows whose email or role column isn't a string. The write path
        # only stores strings, and SQLite's TEXT affinity coerces numeric
        # inserts to text, so in normal operation this is a no-op — but a
        # hand-inserted BLOB (which TEXT affinity leaves as bytes) or a
        # raw-SQL edit could slip a non-string through. Every downstream
        # caller (``AuthService.get_user_role`` / ``all_user_roles`` calling
        # ``email.lower()``, ``admin_dashboard_combined`` iterating ``.items()``
        # and comparing ``role != "revoked"``, the /admin/users role tally)
        # would otherwise AttributeError / TypeError on the non-string and
        # take out the admin UI via the crash handler's empty 503. Matches
        # the isinstance guard PR #152 added on the file backend.
        return {
            row["email"]: row["role"]
            for row in rows
            if isinstance(row["email"], str) and isinstance(row["role"], str)
        }

    def set_user_role(self, email: str, role: str) -> None:
        email = (email or "").lower()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_roles(email, role) VALUES (?, ?)",
                (email, role),
            )

    def delete_user_role(self, email: str) -> bool:
        email = (email or "").lower()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT role FROM user_roles WHERE email = ?", (email,)).fetchone()
            previous = row["role"] if row else None
            conn.execute(
                "INSERT OR REPLACE INTO user_roles(email, role) VALUES (?, ?)",
                (email, "revoked"),
            )
        return previous is not None and previous != "revoked"

    def _replace_user_roles(self, data: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM user_roles")
            for email, role in (data or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO user_roles(email, role) VALUES (?, ?)",
                    (str(email).lower(), str(role)),
                )

    # -------------------------------------------------------- renter_profiles

    def get_renter_profiles(self) -> dict:
        with self.db.read() as conn:
            rows = conn.execute("SELECT email, profile FROM renter_profiles").fetchall()
        # Drop rows whose ``profile`` column doesn't deserialize to a dict.
        # The write path only stores dicts, so in normal operation this is a
        # no-op — but a hand-edited row could hold anything, and every
        # downstream caller (the ``renter_profile`` POST mutating
        # ``profile["name"]``, ``ticket_routes._renter_email_default``
        # calling ``.get("email_status_updates", True)``) would otherwise
        # TypeError / AttributeError and take out the page via the crash
        # handler's 503. Matches the guard the FileStorageService counterpart
        # applies and the same-shape guards on the tickets / lead-captures
        # tables landed in PRs #146 / #147.
        out: dict = {}
        for row in rows:
            profile = self._safe_loads(row["profile"], source="renter_profiles")
            if isinstance(profile, dict):
                out[row["email"]] = profile
        return out

    def save_renter_profiles(self, profiles: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM renter_profiles")
            for email, profile in (profiles or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO renter_profiles(email, profile) VALUES (?, ?)",
                    (str(email).lower(), dumps(profile)),
                )

    # ------------------------------------------------------- renter_contracts

    def get_renter_contracts(self) -> dict:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT email, idx, contract FROM renter_contracts ORDER BY email, idx"
            ).fetchall()
        # Drop rows whose ``contract`` column doesn't deserialize to a dict.
        # The write path only stores dicts, so in normal operation this is a
        # no-op — but a hand-edited row could hold anything, and every
        # downstream caller (``admin_contracts`` doing ``.append``,
        # ``_backfill_contract_ids`` iterating with ``.get("id")``, the CSV
        # export walking ``.get(...)`` per row) would otherwise crash and
        # take out the admin UI via the crash handler's 503. Matches the
        # FileStorageService counterpart and the guards PRs #146 / #147
        # added for tickets and lead captures.
        out: dict[str, list] = {}
        for row in rows:
            contract = self._safe_loads(row["contract"], source="renter_contracts")
            if isinstance(contract, dict):
                out.setdefault(row["email"], []).append(contract)
        return out

    def save_renter_contracts(self, contracts: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM renter_contracts")
            for email, items in (contracts or {}).items():
                email_lc = str(email).lower()
                for idx, contract in enumerate(items or []):
                    conn.execute(
                        "INSERT INTO renter_contracts(email, idx, contract) VALUES (?, ?, ?)",
                        (email_lc, idx, dumps(contract)),
                    )

    # ---------------------------------------------------------------- tickets

    def _load_tickets(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT payload FROM tickets ORDER BY COALESCE(updated_at, created_at) DESC"
            ).fetchall()
        # Filter out any payload that doesn't deserialize to a dict. The
        # write path (``_save_tickets``) only stores dicts, so in normal
        # operation this is a no-op — but a hand-edited row could hold
        # anything, and every downstream caller in ``TicketService`` calls
        # ``.get(...)`` on each entry, which would AttributeError and 503
        # the ticket / dashboard routes via the crash handler. Matches the
        # same guard ``TicketService._load`` applies for the file backend.
        # ``_safe_loads`` also swallows malformed JSON so a corrupted
        # ``payload`` column can't take the caller down before the dict
        # filter runs.
        return [
            payload
            for payload in (self._safe_loads(row["payload"], source="tickets") for row in rows)
            if isinstance(payload, dict)
        ]

    def _save_tickets(self, tickets: list[dict]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM tickets")
            for ticket in tickets or []:
                tid = ticket.get("id")
                if not tid:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO tickets(id, payload, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        str(tid),
                        json.dumps(ticket),
                        ticket.get("created_at"),
                        ticket.get("updated_at"),
                    ),
                )

    # ----------------------------------------------------------- lead_captures

    def get_pending_lead_captures(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT payload FROM lead_captures ORDER BY email"
            ).fetchall()
        # Same isinstance(dict) guard as ``get_pending_registrations`` — a
        # payload column corrupted to non-object JSON would otherwise crash
        # the dedup check in ``add_pending_lead_capture`` and the filter in
        # ``remove_pending_lead_capture`` with AttributeError. ``_safe_loads``
        # also swallows malformed JSON so a truncated / corrupted row can't
        # take the caller down before the dict filter runs.
        return [
            payload
            for payload in (self._safe_loads(row["payload"], source="lead_captures") for row in rows)
            if isinstance(payload, dict)
        ]

    def add_pending_lead_capture(self, lead: dict) -> bool:
        # Mirror FileStorageService: de-duplicate by email so a repeated
        # submission doesn't bloat the table or flood the admin UI. Returns
        # True when newly inserted, False on duplicate or missing email — the
        # caller relies on the bool to skip the "new lead" admin email when
        # the address is already pending.
        target_email = (lead.get("email") or "").strip().lower()
        if not target_email:
            return False
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM lead_captures WHERE email = ?", (target_email,)
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO lead_captures(email, payload) VALUES (?, ?)",
                (target_email, dumps(lead)),
            )
        return True

    def remove_pending_lead_capture(self, email: str) -> None:
        email_lc = (email or "").strip().lower()
        if not email_lc:
            return
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM lead_captures WHERE email = ?", (email_lc,))

    def _replace_pending_lead_captures(self, data: list[dict]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM lead_captures")
            for item in data or []:
                email = (item.get("email") or "").strip().lower()
                if not email:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO lead_captures(email, payload) VALUES (?, ?)",
                    (email, dumps(item)),
                )

    # --------------------------------------------------------- hidden listings

    def get_hidden_listing_ids(self) -> list[str]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT property_id FROM hidden_listings ORDER BY property_id"
            ).fetchall()
        return [row["property_id"] for row in rows]

    def set_listing_hidden(self, property_id: str, hidden: bool) -> None:
        property_id = str(property_id)
        with self.db.transaction() as conn:
            if hidden:
                conn.execute(
                    "INSERT OR REPLACE INTO hidden_listings(property_id) VALUES (?)",
                    (property_id,),
                )
            else:
                conn.execute(
                    "DELETE FROM hidden_listings WHERE property_id = ?", (property_id,)
                )

    def _replace_hidden_listings(self, data: list) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM hidden_listings")
            for property_id in data or []:
                conn.execute(
                    "INSERT OR REPLACE INTO hidden_listings(property_id) VALUES (?)",
                    (str(property_id),),
                )

    # --------------------------------------------------------------- binaries
    #
    # Binary attachments (signed-contract PDFs at
    # ``private/contracts/<uuid>.pdf`` and ticket photos at
    # ``static/uploads/tickets/<ticket_id>/``) are URL-addressed from inside
    # ticket / contract JSON payloads, so they continue to live on disk even
    # when ``USE_SQLITE_STORAGE=1``. These mirror the implementation in
    # ``FileStorageService`` (atomic temp-file + os.replace + fsync) so the
    # request handlers don't need to know which backend is wired up.

    def save_binary_file(self, path, data: bytes) -> bool:
        """Persist ``data`` to ``path`` atomically. Returns True on success."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except Exception as exc:
            self.logger.error("Failed to save binary file %s: %s", path, exc)
            return False

    def load_binary_file(self, path) -> bytes | None:
        try:
            if not path.exists():
                return None
            with path.open("rb") as handle:
                return handle.read()
        except Exception as exc:
            self.logger.error("Failed to load binary file %s: %s", path, exc)
            return None

    def delete_file(self, path) -> bool:
        try:
            if path.exists():
                os.unlink(path)
                return True
        except Exception as exc:
            self.logger.error("Failed to delete file %s: %s", path, exc)
        return False
