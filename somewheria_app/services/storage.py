import contextlib
import json
import os
import tempfile
import threading

from .console import get_console_logger


class FileStorageService:
    def __init__(self, config) -> None:
        self.config = config
        # Re-entrant so callers can wrap a load+modify+save sequence in
        # ``with storage.atomic():`` and still call ``load_json_file`` /
        # ``save_json_file`` (which each acquire the lock) inside the block.
        # Without atomic load+save, two threads doing read-modify-write on the
        # same file silently lose one update — verified: 50 concurrent
        # ``add_pending_registration`` calls under the old plain Lock kept
        # only ~8 entries on disk.
        self.file_lock = threading.RLock()
        self.logger = get_console_logger("storage")

    @contextlib.contextmanager
    def atomic(self):
        """Hold the storage lock across a multi-step read-modify-write.

        SqlStorageService exposes the same context manager (as a no-op,
        since its writes already go through real transactions) so callers
        can rely on this API without branching on the backend.
        """
        with self.file_lock:
            yield

    def load_json_file(self, path, default, *, expected_type: type | tuple[type, ...] | None = None):
        try:
            with self.file_lock:
                if path.exists():
                    with path.open("r", encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    # If the caller declared an expected shape, fall back to
                    # the default when the file's contents don't match. Without
                    # this, a corrupted or hand-edited file holding a dict
                    # where a list is expected would crash callers that do
                    # ``.append`` or list-iteration on the return value.
                    if expected_type is not None and not isinstance(loaded, expected_type):
                        self.logger.warning(
                            "Unexpected JSON shape in %s (got %s); using default",
                            path,
                            type(loaded).__name__,
                        )
                        return default
                    return loaded
        except Exception as exc:
            self.logger.error("Failed to load %s: %s", path, exc)
        return default

    def save_json_file(self, path, data) -> None:
        try:
            with self.file_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(data, handle, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, path)
                except Exception:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
        except Exception as exc:
            self.logger.error("Failed to save %s: %s", path, exc)

    def get_pending_registrations(self) -> list[dict]:
        raw = self.load_json_file(self.config.registration_file, [], expected_type=list)
        # Drop non-dict entries defensively. ``expected_type=list`` only
        # verifies the top-level container; a corrupted / hand-edited file
        # can still slip a bare string, number, ``null``, or list into the
        # array. Every downstream caller (``add_pending_registration`` dedup,
        # ``remove_pending_registration`` filter, the ``admin_registrations``
        # route iteration, the admin template render) calls ``.get("email")``
        # on each item, which would AttributeError and take out the admin
        # UI via the crash handler's 503. Mirrors the isinstance(dict) guard
        # ``recent_listing_activity`` added in PR #144 for the change log.
        return [item for item in raw if isinstance(item, dict)]

    def add_pending_registration(self, registration: dict) -> bool:
        """Append a pending registration, skipping duplicate emails.

        Returns True when a new row was stored, False when the email is
        missing/blank or an entry for the same email already existed.
        De-duplicating here (rather than only in the route) keeps a repeated
        submission from bloating the file or triggering a second admin
        notification, mirroring the SQL backend's identical contract.

        Rejecting missing-email entries matches ``SqlStorageService`` —
        without the guard, a bug in a future caller (or a hand-crafted POST
        slipping past the route-level ``is_valid_email`` check) could pile
        anonymous rows into the JSON file forever, and the admin page would
        show entries no one can ever approve.

        The load+save runs under ``self.file_lock`` so two concurrent
        submissions cannot both load the pre-write list and then race each
        other's saves (which would silently drop one entry).
        """
        target_email = (registration.get("email") or "").strip().lower()
        if not target_email:
            return False
        with self.file_lock:
            registrations = self.get_pending_registrations()
            if any(
                (item.get("email") or "").lower() == target_email for item in registrations
            ):
                return False
            registrations.append(registration)
            self.save_json_file(self.config.registration_file, registrations)
            return True

    def remove_pending_registration(self, email: str) -> None:
        target = (email or "").lower()
        with self.file_lock:
            # ``(item.get("email") or "")`` — not ``item.get("email", "")`` —
            # because ``.get`` returns the stored value (even ``None``) when
            # the key is present, so a corrupted / hand-edited row of
            # ``{"email": null, ...}`` would otherwise crash the comparison
            # with ``AttributeError: 'NoneType' object has no attribute
            # 'lower'`` and take out /admin/registrations approve/reject via
            # the crash handler's empty 503. Same idiom
            # ``add_pending_registration`` already uses on the dedup path.
            registrations = [
                item for item in self.get_pending_registrations()
                if (item.get("email") or "").lower() != target
            ]
            self.save_json_file(self.config.registration_file, registrations)

    def get_user_roles(self) -> dict:
        raw = self.load_json_file(self.config.user_roles_file, {}, expected_type=dict)
        # Drop entries whose email key or role value isn't a string.
        # ``expected_type=dict`` only verifies the top-level container; a
        # corrupted / hand-edited file can still slip a non-string key (an
        # integer, ``null``) or a non-string value (a nested dict, list, or
        # bool) into the mapping. Every downstream caller (``AuthService.
        # all_user_roles`` calling ``email.lower()``, ``admin_dashboard_combined``
        # iterating ``.items()`` and comparing ``role != "revoked"``, the
        # /admin/users role tally) would otherwise AttributeError / TypeError
        # on the non-string and take out the admin UI via the crash handler's
        # empty 503. Mirrors the isinstance guards PRs #146 / #147 / #148
        # added for pending registrations, tickets, and renter profiles.
        return {
            email: role
            for email, role in raw.items()
            if isinstance(email, str) and isinstance(role, str)
        }

    def set_user_role(self, email: str, role: str) -> None:
        with self.file_lock:
            roles = self.get_user_roles()
            roles[email.lower()] = role
            self.save_json_file(self.config.user_roles_file, roles)

    def delete_user_role(self, email: str) -> bool:
        email = email.lower()
        with self.file_lock:
            roles = self.get_user_roles()
            previous = roles.get(email)
            # Store a tombstone ("revoked") instead of removing the key outright
            # so that env-var fallbacks in AuthService.get_user_role cannot
            # silently restore a deleted user's access on their next login.
            roles[email] = "revoked"
            self.save_json_file(self.config.user_roles_file, roles)
        return previous is not None and previous != "revoked"

    def get_renter_profiles(self) -> dict:
        raw = self.load_json_file(self.config.renter_profile_file, {}, expected_type=dict)
        # Drop non-dict profile values defensively. ``expected_type=dict`` only
        # verifies the top-level container; a corrupted / hand-edited file can
        # still slip a bare string, number, ``null``, or list under an email
        # key. Every downstream caller (``renter_profile`` route mutating
        # ``profile["name"]``, ``ticket_routes._renter_email_default`` calling
        # ``.get("email_status_updates", True)``) would otherwise TypeError /
        # AttributeError and take out the page via the crash handler's 503.
        # Mirrors the isinstance(dict) guard PRs #146 / #147 added for pending
        # registrations, lead captures, and tickets.
        return {email: profile for email, profile in raw.items() if isinstance(profile, dict)}

    def save_renter_profiles(self, profiles: dict) -> None:
        self.save_json_file(self.config.renter_profile_file, profiles)

    def get_pending_lead_captures(self) -> list[dict]:
        raw = self.load_json_file(self.config.lead_capture_file, [], expected_type=list)
        # Same isinstance(dict) guard as ``get_pending_registrations``. Without
        # it, a stray non-dict row (corrupted or hand-edited file) crashes the
        # dedup check in ``add_pending_lead_capture`` and the filter in
        # ``remove_pending_lead_capture`` with AttributeError.
        return [item for item in raw if isinstance(item, dict)]

    def add_pending_lead_capture(self, lead: dict) -> bool:
        # Returns True when the lead was newly persisted, False when the
        # email is missing/blank or it was rejected as a duplicate. The
        # caller uses the return value to decide whether to fire the
        # "new lead" admin email — without that gate a repeated submission
        # of an already-pending address spams the inbox. Rejecting
        # missing-email entries matches ``SqlStorageService`` so behavior
        # is identical whether the JSON file or the SQLite backend is
        # active (the storage layer is feature-flagged via USE_SQLITE_STORAGE).
        # Load+save runs under the file lock so two concurrent submissions
        # can't both pass the dedup check and then race each other's saves.
        target_email = (lead.get("email") or "").strip().lower()
        if not target_email:
            return False
        with self.file_lock:
            leads = self.get_pending_lead_captures()
            # De-duplicate by email so a repeated submission doesn't bloat the file
            # or give the requester a way to flood the admin UI.
            # ``(item.get("email") or "")`` — see ``remove_pending_registration``:
            # a stored ``{"email": null}`` row would otherwise crash the dedup
            # walk with AttributeError on ``None.lower()``.
            if any((item.get("email") or "").lower() == target_email for item in leads):
                return False
            leads.append(lead)
            self.save_json_file(self.config.lead_capture_file, leads)
            return True

    def remove_pending_lead_capture(self, email: str) -> None:
        target = (email or "").lower()
        with self.file_lock:
            # ``(item.get("email") or "")`` — see ``remove_pending_registration``:
            # a stored ``{"email": null}`` row would otherwise crash the filter
            # with AttributeError on ``None.lower()`` and swallow the admin
            # response via the crash handler's empty 503.
            leads = [
                item for item in self.get_pending_lead_captures()
                if (item.get("email") or "").lower() != target
            ]
            self.save_json_file(self.config.lead_capture_file, leads)

    # ---------------------------------------------------- hidden listings
    #
    # Property data lives upstream, but the upstream table has no
    # active/inactive column — "deactivated" (hidden from the public site,
    # kept on the books) is state this site owns, so it persists here like
    # the other JSON-backed state.

    def get_hidden_listing_ids(self) -> list[str]:
        ids = self.load_json_file(self.config.hidden_listings_file, [], expected_type=list)
        # Drop entries that aren't non-empty strings. ``expected_type=list``
        # only verifies the top-level container; a corrupted / hand-edited
        # file can still slip a ``null``, a bare number, a nested dict, or
        # an empty string into the array. A bare ``[str(item) for item in
        # ids]`` would turn those into "None", "42", "{}" — junk strings
        # that never match a real property id but still sit in the hidden
        # set forever (or, worse, an empty string that silently hides every
        # property whose id round-trips to ""). Matches the isinstance
        # guards PRs #144-#153 added on the change log, tickets, user
        # roles, pending registrations, lead captures, and profiles.
        return [item for item in ids if isinstance(item, str) and item]

    def set_listing_hidden(self, property_id: str, hidden: bool) -> None:
        property_id = str(property_id)
        with self.file_lock:
            ids = self.get_hidden_listing_ids()
            if hidden:
                if property_id not in ids:
                    ids.append(property_id)
            else:
                ids = [item for item in ids if item != property_id]
            self.save_json_file(self.config.hidden_listings_file, ids)

    def get_renter_contracts(self) -> dict:
        raw = self.load_json_file(self.config.contracts_file, {}, expected_type=dict)
        # Drop non-list values and non-dict contract items defensively.
        # ``expected_type=dict`` only verifies the top-level container; a
        # corrupted / hand-edited file can still slip a scalar or a list of
        # non-dict entries under an email key. Every downstream caller
        # (``admin_contracts`` doing ``setdefault(...).append(...)``,
        # ``_backfill_contract_ids`` iterating with ``.get("id")``, the CSV
        # export walking ``.get(...)`` per row) would otherwise crash and
        # take out the admin UI via the crash handler's 503. Matches the
        # isinstance(dict) guard PRs #146 / #147 added for pending
        # registrations, lead captures, and tickets.
        cleaned: dict = {}
        for email, contracts in raw.items():
            if not isinstance(contracts, list):
                continue
            cleaned[email] = [item for item in contracts if isinstance(item, dict)]
        return cleaned

    def save_renter_contracts(self, contracts: dict) -> None:
        self.save_json_file(self.config.contracts_file, contracts)

    # --- Binary file persistence (contract PDFs, ticket photos) ---
    #
    # These mirror the JSON helpers: writes go via tempfile + os.replace so a
    # crash mid-write can't leave a half-written file on disk. The single
    # in-process lock serializes all storage I/O — fine for a single-worker
    # deployment, would need to move to a real DB for multi-worker.

    def save_binary_file(self, path, data: bytes) -> bool:
        """Persist ``data`` to ``path`` atomically. Returns True on success."""
        try:
            with self.file_lock:
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
            with self.file_lock:
                if not path.exists():
                    return None
                with path.open("rb") as handle:
                    return handle.read()
        except Exception as exc:
            self.logger.error("Failed to load binary file %s: %s", path, exc)
            return None

    def delete_file(self, path) -> bool:
        try:
            with self.file_lock:
                if path.exists():
                    os.unlink(path)
                    return True
        except Exception as exc:
            self.logger.error("Failed to delete file %s: %s", path, exc)
        return False
