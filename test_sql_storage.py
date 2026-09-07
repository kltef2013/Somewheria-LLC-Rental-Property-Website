"""End-to-end tests for ``SqlStorageService``.

These exercise the SQLite-backed storage backend that activates when
``USE_SQLITE_STORAGE=1``. The scaffold landed without test coverage and
subsequently fell behind ``FileStorageService`` (it was missing lead-capture
and binary-file methods). These tests pin both the schema and the public
API parity so the feature flag is safe to flip in production.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from somewheria_app.services.sql_storage import SqlStorageService


def _make_config(base: Path) -> SimpleNamespace:
    return SimpleNamespace(
        registration_file=base / "pending_registrations.json",
        user_roles_file=base / "user_roles.json",
        renter_profile_file=base / "renter_profiles.json",
        contracts_file=base / "renter_contracts.json",
        tickets_file=base / "tickets.json",
        lead_capture_file=base / "pending_lead_captures.json",
        # ``SqlStorageService._path_key`` compares the incoming path against
        # every configured storage file, including the hidden-listings JSON,
        # so this attribute has to be present for the unknown-path shim tests
        # to even reach their assertion. Adding a new storage bucket without
        # extending this fixture broke CI on main; keep every file the shim
        # references listed here.
        hidden_listings_file=base / "hidden_listings.json",
        sqlite_file=base / "test.sqlite3",
    )


class SqlStorageBaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.config = _make_config(self.base)
        self.storage = SqlStorageService(self.config)


class UserRolesTestCase(SqlStorageBaseTestCase):
    def test_set_and_get_role(self):
        self.storage.set_user_role("Admin@Example.com", "admin")
        roles = self.storage.get_user_roles()
        self.assertEqual(roles, {"admin@example.com": "admin"})

    def test_delete_user_role_records_tombstone(self):
        self.storage.set_user_role("user@example.com", "renter")
        existed = self.storage.delete_user_role("user@example.com")
        self.assertTrue(existed)
        # Tombstone "revoked" must persist so env-var fallbacks can't silently
        # restore access. The role isn't removed outright.
        self.assertEqual(
            self.storage.get_user_roles(), {"user@example.com": "revoked"}
        )

    def test_delete_user_role_returns_false_when_absent(self):
        existed = self.storage.delete_user_role("ghost@example.com")
        self.assertFalse(existed)

    def test_get_user_roles_drops_non_string_email_or_role(self):
        # SQLite's TEXT affinity coerces numeric inserts to text, but a raw
        # BLOB inserted into a TEXT column round-trips back as ``bytes`` — a
        # hand-inserted BLOB (or a raw-SQL edit slipping some other type in
        # via a schema tweak) would otherwise reach ``AuthService.get_user_role``,
        # whose ``email.lower()`` crashes on non-strings and takes out the
        # admin UI via the crash handler's empty 503. Matches the isinstance
        # guard PR #152 added on the file backend.
        self.storage.set_user_role("keep@example.com", "admin")
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO user_roles(email, role) VALUES (?, ?)",
                (b"blob-email@example.com", "renter"),
            )
            conn.execute(
                "INSERT INTO user_roles(email, role) VALUES (?, ?)",
                ("blob-role@example.com", b"renter"),
            )
        self.assertEqual(
            self.storage.get_user_roles(), {"keep@example.com": "admin"}
        )


class PendingRegistrationsTestCase(SqlStorageBaseTestCase):
    def test_add_and_get(self):
        self.storage.add_pending_registration(
            {"email": "Alice@Example.com", "name": "Alice"}
        )
        rows = self.storage.get_pending_registrations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "Alice@Example.com")
        self.assertEqual(rows[0]["name"], "Alice")

    def test_remove_pending_registration(self):
        self.storage.add_pending_registration({"email": "a@example.com"})
        self.storage.add_pending_registration({"email": "b@example.com"})
        self.storage.remove_pending_registration("A@Example.com")
        rows = self.storage.get_pending_registrations()
        self.assertEqual({r["email"] for r in rows}, {"b@example.com"})

    def test_add_pending_registration_dedupes_and_reports_new(self):
        self.assertTrue(
            self.storage.add_pending_registration({"email": "Dup@Example.com", "name": "First"})
        )
        self.assertFalse(
            self.storage.add_pending_registration({"email": "dup@example.com", "name": "Second"})
        )
        rows = self.storage.get_pending_registrations()
        self.assertEqual(len(rows), 1)
        # The original payload is preserved; the duplicate does not overwrite it.
        self.assertEqual(rows[0]["name"], "First")

    def test_get_pending_registrations_drops_non_dict_payloads(self):
        # A payload column that deserializes to something other than a dict
        # (e.g. hand-edited row storing ``"null"``, ``"42"``, or a JSON array)
        # must not crash the admin UI when it iterates ``.get("email")`` on
        # each entry. Matches the isinstance(dict) guard on the file backend
        # and the same-shape guard PR #144 added to ``recent_listing_activity``.
        self.storage.add_pending_registration({"email": "keep@example.com"})
        # Bypass the service so we can insert a deliberately malformed payload
        # into the underlying table without the API path rejecting it.
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_registrations(email, payload) VALUES (?, ?)",
                ("garbage@example.com", '"not a dict"'),
            )
            conn.execute(
                "INSERT INTO pending_registrations(email, payload) VALUES (?, ?)",
                ("null@example.com", "null"),
            )
        rows = self.storage.get_pending_registrations()
        self.assertEqual(rows, [{"email": "keep@example.com"}])


class RenterProfilesTestCase(SqlStorageBaseTestCase):
    def test_save_and_get(self):
        self.storage.save_renter_profiles(
            {"renter@example.com": {"name": "Renter R"}}
        )
        profiles = self.storage.get_renter_profiles()
        self.assertEqual(profiles, {"renter@example.com": {"name": "Renter R"}})

    def test_get_renter_profiles_drops_non_dict_payloads(self):
        # A ``profile`` column that deserializes to something other than a
        # dict (a hand-edited row storing ``"null"``, a JSON array, or a
        # scalar) must not crash the ``renter_profile`` route or the
        # ``_renter_email_default`` helper, both of which call ``.get(...)``
        # / mutate keys on each value. Matches the FileStorageService guard
        # and the same-shape guard on the tickets / lead-captures tables
        # landed in PRs #146 / #147.
        self.storage.save_renter_profiles(
            {"keep@example.com": {"name": "Renter R"}}
        )
        # Bypass the service so we can insert deliberately malformed payloads
        # into the underlying table.
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO renter_profiles(email, profile) VALUES (?, ?)",
                ("garbage@example.com", '"not a dict"'),
            )
            conn.execute(
                "INSERT INTO renter_profiles(email, profile) VALUES (?, ?)",
                ("null@example.com", "null"),
            )
            conn.execute(
                "INSERT INTO renter_profiles(email, profile) VALUES (?, ?)",
                ("list@example.com", "[1, 2, 3]"),
            )
        profiles = self.storage.get_renter_profiles()
        self.assertEqual(profiles, {"keep@example.com": {"name": "Renter R"}})


class RenterContractsTestCase(SqlStorageBaseTestCase):
    def test_save_and_get_preserves_order(self):
        contracts = {
            "renter@example.com": [
                {"contract_id": "a"},
                {"contract_id": "b"},
                {"contract_id": "c"},
            ]
        }
        self.storage.save_renter_contracts(contracts)
        out = self.storage.get_renter_contracts()
        self.assertEqual(
            [c["contract_id"] for c in out["renter@example.com"]],
            ["a", "b", "c"],
        )

    def test_get_renter_contracts_drops_non_dict_payloads(self):
        # A ``contract`` column that deserializes to something other than a
        # dict (a hand-edited row storing ``"null"``, a JSON array, or a
        # scalar) must not crash the admin contracts UI, the CSV export, or
        # ``_backfill_contract_ids`` — all iterate with ``.get(...)`` per
        # entry. Matches the FileStorageService guard and the same-shape
        # guards on the tickets / lead-captures tables landed in PRs #146 /
        # #147.
        self.storage.save_renter_contracts(
            {"renter@example.com": [{"id": "c1"}]}
        )
        # Bypass the service so we can insert deliberately malformed
        # contract rows straight into the underlying table.
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO renter_contracts(email, idx, contract) VALUES (?, ?, ?)",
                ("renter@example.com", 1, '"not a dict"'),
            )
            conn.execute(
                "INSERT INTO renter_contracts(email, idx, contract) VALUES (?, ?, ?)",
                ("renter@example.com", 2, "null"),
            )
            conn.execute(
                "INSERT INTO renter_contracts(email, idx, contract) VALUES (?, ?, ?)",
                ("renter@example.com", 3, "[1, 2, 3]"),
            )
            conn.execute(
                "INSERT INTO renter_contracts(email, idx, contract) VALUES (?, ?, ?)",
                ("other@example.com", 0, '"junk"'),
            )
        out = self.storage.get_renter_contracts()
        # Only the well-formed contract survives; ``other@example.com`` has no
        # dict rows and shouldn't appear at all so downstream lookups don't
        # accidentally see an empty renter entry.
        self.assertEqual(out, {"renter@example.com": [{"id": "c1"}]})


class TicketsTestCase(SqlStorageBaseTestCase):
    def test_save_and_load_via_path_shim(self):
        # TicketService calls load_json_file / save_json_file directly with
        # config.tickets_file. The shim must route to the tickets table.
        tickets = [
            {"id": "t1", "title": "leak", "created_at": "2026-01-01"},
            {"id": "t2", "title": "lock", "created_at": "2026-01-02"},
        ]
        self.storage.save_json_file(self.config.tickets_file, tickets)
        loaded = self.storage.load_json_file(self.config.tickets_file, [])
        self.assertEqual(sorted(t["id"] for t in loaded), ["t1", "t2"])

    def test_load_tickets_drops_non_dict_payloads(self):
        # A payload column that deserializes to something other than a dict
        # (a hand-edited row storing ``"null"``, ``"42"``, or a JSON array)
        # must not crash TicketService callers when they iterate
        # ``.get(...)`` on each ticket. Matches the isinstance(dict) guard
        # ``TicketService._load`` applies for the file backend and the same
        # shape guard added for pending registrations in PR #146.
        self.storage.save_json_file(
            self.config.tickets_file,
            [{"id": "t1", "title": "leak", "created_at": "2026-01-01"}],
        )
        # Bypass the service so we can insert a deliberately malformed payload
        # straight into the underlying table.
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO tickets(id, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("bad-string", '"not a dict"', "2026-01-02", None),
            )
            conn.execute(
                "INSERT INTO tickets(id, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("bad-null", "null", "2026-01-03", None),
            )
            conn.execute(
                "INSERT INTO tickets(id, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("bad-list", "[1, 2, 3]", "2026-01-04", None),
            )
        loaded = self.storage.load_json_file(self.config.tickets_file, [])
        self.assertEqual([t["id"] for t in loaded], ["t1"])


class LeadCapturesTestCase(SqlStorageBaseTestCase):
    """Regression coverage for the lead-capture flow under SQLite."""

    def test_add_pending_lead_capture(self):
        added = self.storage.add_pending_lead_capture(
            {"email": "lead@example.com", "submitted_at": "2026-01-01"}
        )
        self.assertTrue(added)
        leads = self.storage.get_pending_lead_captures()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["email"], "lead@example.com")
        self.assertEqual(leads[0]["submitted_at"], "2026-01-01")

    def test_add_pending_lead_capture_dedupes_existing_email(self):
        # Mirror FileStorageService: repeated submissions don't bloat storage,
        # and the second insert must report False so the public route can
        # suppress the admin notification email on duplicates.
        first = self.storage.add_pending_lead_capture(
            {"email": "dup@example.com", "submitted_at": "2026-01-01"}
        )
        second = self.storage.add_pending_lead_capture(
            {"email": "dup@example.com", "submitted_at": "2026-02-02"}
        )
        self.assertTrue(first)
        self.assertFalse(second)
        leads = self.storage.get_pending_lead_captures()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["submitted_at"], "2026-01-01")

    def test_add_pending_lead_capture_dedupe_is_case_insensitive(self):
        self.storage.add_pending_lead_capture({"email": "Mixed@Example.com"})
        self.storage.add_pending_lead_capture({"email": "mixed@example.com"})
        self.assertEqual(len(self.storage.get_pending_lead_captures()), 1)

    def test_add_pending_lead_capture_ignores_missing_email(self):
        self.storage.add_pending_lead_capture({"submitted_at": "2026-01-01"})
        self.assertEqual(self.storage.get_pending_lead_captures(), [])

    def test_remove_pending_lead_capture(self):
        self.storage.add_pending_lead_capture({"email": "keep@example.com"})
        self.storage.add_pending_lead_capture({"email": "drop@example.com"})
        self.storage.remove_pending_lead_capture("DROP@Example.com")
        leads = self.storage.get_pending_lead_captures()
        self.assertEqual({lead["email"] for lead in leads}, {"keep@example.com"})

    def test_get_pending_lead_captures_drops_non_dict_payloads(self):
        # Same isinstance(dict) guard as pending registrations: a payload
        # column corrupted to a non-object JSON value must not crash the
        # dedup/remove paths with AttributeError on ``.get("email")``.
        self.storage.add_pending_lead_capture({"email": "keep@example.com"})
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO lead_captures(email, payload) VALUES (?, ?)",
                ("garbage@example.com", '"not a dict"'),
            )
        leads = self.storage.get_pending_lead_captures()
        self.assertEqual(leads, [{"email": "keep@example.com"}])

    def test_remove_pending_lead_capture_no_op_on_empty_email(self):
        self.storage.add_pending_lead_capture({"email": "keep@example.com"})
        self.storage.remove_pending_lead_capture("")
        self.assertEqual(len(self.storage.get_pending_lead_captures()), 1)

    def test_load_via_path_shim(self):
        # Public callers go through the shim with config.lead_capture_file.
        self.storage.add_pending_lead_capture({"email": "lead@example.com"})
        loaded = self.storage.load_json_file(self.config.lead_capture_file, [])
        self.assertEqual([lead["email"] for lead in loaded], ["lead@example.com"])

    def test_save_via_path_shim_replaces_table(self):
        self.storage.add_pending_lead_capture({"email": "old@example.com"})
        self.storage.save_json_file(
            self.config.lead_capture_file,
            [{"email": "new@example.com", "submitted_at": "2026-03-03"}],
        )
        loaded = self.storage.get_pending_lead_captures()
        self.assertEqual([lead["email"] for lead in loaded], ["new@example.com"])


class BinaryFileTestCase(SqlStorageBaseTestCase):
    """Binary attachments stay on disk even under SQLite storage."""

    def test_save_load_round_trip(self):
        path = self.base / "uploads" / "contracts" / "abc.pdf"
        ok = self.storage.save_binary_file(path, b"%PDF-1.4 fake")
        self.assertTrue(ok)
        self.assertEqual(self.storage.load_binary_file(path), b"%PDF-1.4 fake")

    def test_save_creates_parent_directories(self):
        # The contracts/tickets upload directories are pre-created at startup
        # by AppConfig.ensure_directories, but each ticket gets its own
        # subdirectory at upload time. mkdir(parents=True) is required.
        path = self.base / "uploads" / "tickets" / "abc123" / "photo.jpg"
        ok = self.storage.save_binary_file(path, b"\xff\xd8\xff\xe0")
        self.assertTrue(ok)
        self.assertTrue(path.exists())

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.storage.load_binary_file(self.base / "nope.bin"))

    def test_delete_file_removes_existing(self):
        path = self.base / "delete_me.bin"
        self.storage.save_binary_file(path, b"data")
        self.assertTrue(self.storage.delete_file(path))
        self.assertFalse(path.exists())

    def test_delete_file_returns_false_when_absent(self):
        self.assertFalse(self.storage.delete_file(self.base / "ghost.bin"))


class HiddenListingsTestCase(SqlStorageBaseTestCase):
    """The hidden-listings bucket landed without SQL-backend tests. Cover the
    round-trip, the toggle semantics, and the path-shim wiring so future
    additions to ``_path_key`` can't regress the shim silently again."""

    def test_set_listing_hidden_round_trip(self):
        self.storage.set_listing_hidden("prop-1", True)
        self.assertEqual(self.storage.get_hidden_listing_ids(), ["prop-1"])

    def test_set_listing_hidden_is_idempotent(self):
        self.storage.set_listing_hidden("prop-1", True)
        self.storage.set_listing_hidden("prop-1", True)
        self.assertEqual(self.storage.get_hidden_listing_ids(), ["prop-1"])

    def test_set_listing_hidden_false_removes_row(self):
        self.storage.set_listing_hidden("prop-1", True)
        self.storage.set_listing_hidden("prop-1", False)
        self.assertEqual(self.storage.get_hidden_listing_ids(), [])

    def test_get_hidden_listing_ids_sorted(self):
        for pid in ("c", "a", "b"):
            self.storage.set_listing_hidden(pid, True)
        self.assertEqual(self.storage.get_hidden_listing_ids(), ["a", "b", "c"])

    def test_load_via_path_shim(self):
        self.storage.set_listing_hidden("prop-1", True)
        loaded = self.storage.load_json_file(self.config.hidden_listings_file, [])
        self.assertEqual(loaded, ["prop-1"])

    def test_save_via_path_shim_replaces_table(self):
        self.storage.set_listing_hidden("stale", True)
        self.storage.save_json_file(
            self.config.hidden_listings_file, ["fresh-1", "fresh-2"]
        )
        self.assertEqual(
            self.storage.get_hidden_listing_ids(), ["fresh-1", "fresh-2"]
        )


class PathShimUnknownPathTestCase(SqlStorageBaseTestCase):
    """An unrecognised path must NOT silently corrupt or crash."""

    def test_unknown_load_returns_default(self):
        out = self.storage.load_json_file(self.base / "unknown.json", [])
        self.assertEqual(out, [])

    def test_unknown_save_is_a_no_op(self):
        # Must not raise; the warning is logged but the request flow continues.
        self.storage.save_json_file(self.base / "unknown.json", [{"x": 1}])

    def test_missing_config_attribute_is_treated_as_unknown_path(self):
        # If a config forgets one of the known file attributes (e.g. an
        # older AppConfig deployed against a newer sql_storage), the path
        # shim must fall through to the "unknown path" branch rather than
        # raising AttributeError halfway through the checks. That would
        # abort load/save for every path — including the ones the config
        # DID configure correctly.
        #
        # ``_make_config`` deliberately sets every attribute the shim knows
        # about (other tests in this file route through them), so drop one
        # here to reconstruct the older-config shape this test is about.
        del self.config.hidden_listings_file
        self.assertFalse(hasattr(self.config, "hidden_listings_file"))
        self.assertEqual(self.storage.load_json_file(self.base / "unknown.json", []), [])
        self.storage.save_json_file(self.base / "unknown.json", [{"x": 1}])
        # A path that IS configured must still route correctly.
        self.storage.set_user_role("still-works@example.com", "renter")
        self.assertEqual(
            self.storage.get_user_roles(),
            {"still-works@example.com": "renter"},
        )


class AtomicTestCase(SqlStorageBaseTestCase):
    """``atomic()`` must serialize route-level read-modify-write sequences.

    Individual SQL writes already run inside ``db.transaction()``, but a
    load+save in a route handler spans two transactions and races
    concurrent writers — exactly the lost-update class commit e062313
    fixed for the JSON backend. The SQL backend's ``atomic()`` used to
    be a no-op; it now acquires a process-wide RLock so the same
    ``with storage.atomic():`` wrap fixes both backends.
    """

    def test_atomic_serializes_renter_contracts_updates(self):
        import threading

        N = 16
        barrier = threading.Barrier(N)

        def worker(i: int) -> None:
            barrier.wait()
            with self.storage.atomic():
                contracts = self.storage.get_renter_contracts()
                contracts.setdefault(f"renter{i}@example.com", []).append(
                    {"id": f"c{i}", "property_name": f"P{i}"}
                )
                self.storage.save_renter_contracts(contracts)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = self.storage.get_renter_contracts()
        self.assertEqual(len(stored), N)
        self.assertEqual(
            set(stored),
            {f"renter{i}@example.com" for i in range(N)},
        )

    def test_atomic_is_reentrant(self):
        # FileStorageService.atomic() is re-entrant because file_lock is an
        # RLock; the SQL backend must match so a nested ``with atomic():``
        # block (e.g. a helper that itself calls atomic()) doesn't deadlock.
        with self.storage.atomic():
            with self.storage.atomic():
                self.storage.set_user_role("nested@example.com", "renter")
        self.assertEqual(
            self.storage.get_user_roles(), {"nested@example.com": "renter"}
        )


if __name__ == "__main__":
    unittest.main()
