import datetime
import os
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, mock_open, patch

from somewheria_app.services.appointments import AppointmentService
from somewheria_app.services.auth import AuthService
from somewheria_app.services.notifications import NotificationService
from somewheria_app.services.properties import PropertyService
from somewheria_app.services.storage import FileStorageService
from somewheria_app.services.validation import is_valid_email


class DummyForm:
    def __init__(self, values=None, lists=None):
        self.values = values or {}
        self.lists = lists or {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getlist(self, key):
        return list(self.lists.get(key, []))


class AuthServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.storage = Mock()
        self.config = SimpleNamespace(
            authorized_users={"renter@example.com"},
            admin_users={"admin@example.com"},
            high_admin_users={"owner@example.com"},
        )
        self.service = AuthService(self.config, self.storage)

    def test_whitelist_configured_returns_true_when_authorized_users_exist(self):
        self.assertTrue(self.service.whitelist_configured())

    def test_get_user_role_prefers_storage_role(self):
        self.storage.get_user_roles.return_value = {"user@example.com": "admin"}

        role = self.service.get_user_role("user@example.com")

        self.assertEqual(role, "admin")

    def test_get_user_role_uses_high_admin_config(self):
        self.storage.get_user_roles.return_value = {}

        role = self.service.get_user_role("owner@example.com")

        self.assertEqual(role, "high_admin")

    def test_get_user_role_uses_admin_config(self):
        self.storage.get_user_roles.return_value = {}

        role = self.service.get_user_role("admin@example.com")

        self.assertEqual(role, "admin")

    def test_get_user_role_uses_authorized_users_as_renter(self):
        self.storage.get_user_roles.return_value = {}

        role = self.service.get_user_role("renter@example.com")

        self.assertEqual(role, "renter")

    def test_get_user_role_defaults_to_guest(self):
        self.storage.get_user_roles.return_value = {}

        role = self.service.get_user_role("guest@example.com")

        self.assertEqual(role, "guest")

    def test_all_user_roles_includes_env_configured_admins(self):
        # On a fresh deploy user_roles.json is empty, but the .env-configured
        # admins must still show up on the user-management page.
        self.storage.get_user_roles.return_value = {}

        result = self.service.all_user_roles()

        by_email = {u["email"]: u for u in result}
        self.assertEqual(by_email["owner@example.com"]["role"], "high_admin")
        self.assertEqual(by_email["admin@example.com"]["role"], "admin")
        self.assertEqual(by_email["renter@example.com"]["role"], "renter")
        self.assertTrue(all(u["source"] == "config" for u in result))
        # Sorted by email for a stable table order.
        self.assertEqual([u["email"] for u in result], sorted(by_email))

    def test_all_user_roles_file_overrides_env_and_marks_source(self):
        self.storage.get_user_roles.return_value = {"admin@example.com": "high_admin"}

        by_email = {u["email"]: u for u in self.service.all_user_roles()}

        self.assertEqual(by_email["admin@example.com"]["role"], "high_admin")
        self.assertEqual(by_email["admin@example.com"]["source"], "file")

    def test_all_user_roles_hides_revoked_tombstones(self):
        self.storage.get_user_roles.return_value = {"owner@example.com": "revoked"}

        emails = {u["email"] for u in self.service.all_user_roles()}

        self.assertNotIn("owner@example.com", emails)

    def test_all_user_roles_includes_ui_assigned_user_not_in_env(self):
        self.storage.get_user_roles.return_value = {"new@example.com": "renter"}

        by_email = {u["email"]: u for u in self.service.all_user_roles()}

        self.assertEqual(by_email["new@example.com"]["role"], "renter")
        self.assertEqual(by_email["new@example.com"]["source"], "file")


class FileStorageServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            registration_file=Path("registrations.json"),
            user_roles_file=Path("roles.json"),
            renter_profile_file=Path("profiles.json"),
            contracts_file=Path("contracts.json"),
            lead_capture_file=Path("lead_captures.json"),
            hidden_listings_file=Path("hidden_listings.json"),
        )
        self.service = FileStorageService(self.config)

    def test_load_json_file_returns_default_when_file_is_missing(self):
        with patch.object(Path, "exists", return_value=False):
            loaded = self.service.load_json_file(self.config.registration_file, [])

        self.assertEqual(loaded, [])

    def test_load_json_file_reads_existing_json(self):
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "open",
            mock_open(read_data='{"admin@example.com": "admin"}'),
        ):
            loaded = self.service.load_json_file(self.config.user_roles_file, {})

        self.assertEqual(loaded, {"admin@example.com": "admin"})

    def test_load_json_file_falls_back_when_expected_type_does_not_match(self):
        # File exists and contains valid JSON, but it's a dict where the
        # caller asked for a list. Without the type check, downstream code
        # that does ``.append`` on the return value would crash.
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "open",
            mock_open(read_data='{"oops": "wrong shape"}'),
        ):
            loaded = self.service.load_json_file(
                self.config.registration_file, [], expected_type=list
            )

        self.assertEqual(loaded, [])

    def test_load_json_file_returns_loaded_value_when_type_matches(self):
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "open",
            mock_open(read_data='[{"email": "a@example.com"}]'),
        ):
            loaded = self.service.load_json_file(
                self.config.registration_file, [], expected_type=list
            )

        self.assertEqual(loaded, [{"email": "a@example.com"}])

    def test_set_listing_hidden_appends_new_id(self):
        with patch.object(self.service, "get_hidden_listing_ids", return_value=["prop-1"]), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            self.service.set_listing_hidden("prop-2", True)

        save_json_mock.assert_called_once_with(self.config.hidden_listings_file, ["prop-1", "prop-2"])

    def test_set_listing_hidden_is_idempotent_for_existing_id(self):
        with patch.object(self.service, "get_hidden_listing_ids", return_value=["prop-1"]), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            self.service.set_listing_hidden("prop-1", True)

        save_json_mock.assert_called_once_with(self.config.hidden_listings_file, ["prop-1"])

    def test_set_listing_hidden_false_removes_id(self):
        with patch.object(
            self.service, "get_hidden_listing_ids", return_value=["prop-1", "prop-2"]
        ), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            self.service.set_listing_hidden("prop-1", False)

        save_json_mock.assert_called_once_with(self.config.hidden_listings_file, ["prop-2"])

    def test_add_pending_registration_appends_and_saves(self):
        with patch.object(self.service, "get_pending_registrations", return_value=[{"email": "keep@example.com"}]), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            self.service.add_pending_registration({"email": "new@example.com", "name": "New User"})

        save_json_mock.assert_called_once_with(
            self.config.registration_file,
            [{"email": "keep@example.com"}, {"email": "new@example.com", "name": "New User"}],
        )

    def test_add_pending_registration_returns_true_when_new(self):
        with patch.object(self.service, "get_pending_registrations", return_value=[]), patch.object(
            self.service, "save_json_file"
        ):
            self.assertTrue(
                self.service.add_pending_registration({"email": "new@example.com"})
            )

    def test_add_pending_registration_dedupes_existing_email(self):
        # A repeated submission (case-insensitive) must not re-save or report
        # a new row, so the route won't fire a second admin notification.
        with patch.object(
            self.service, "get_pending_registrations", return_value=[{"email": "dup@example.com"}]
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            result = self.service.add_pending_registration({"email": "DUP@example.com"})
        self.assertFalse(result)
        save_json_mock.assert_not_called()

    def test_add_pending_registration_ignores_missing_email(self):
        # Behaviour parity with ``SqlStorageService`` (see
        # ``test_add_pending_registration_ignores_missing_email`` in
        # test_sql_storage.py): entries missing an email are rejected
        # instead of piling up anonymous rows that no admin can approve.
        with patch.object(
            self.service, "get_pending_registrations", return_value=[]
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.assertFalse(self.service.add_pending_registration({"name": "No Email"}))
            self.assertFalse(self.service.add_pending_registration({"email": "   "}))
            self.assertFalse(self.service.add_pending_registration({"email": None}))
        save_json_mock.assert_not_called()

    def test_remove_pending_registration_deletes_matching_entry(self):
        with patch.object(
            self.service,
            "get_pending_registrations",
            return_value=[{"email": "keep@example.com"}, {"email": "drop@example.com"}],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.remove_pending_registration("drop@example.com")

        save_json_mock.assert_called_once_with(
            self.config.registration_file,
            [{"email": "keep@example.com"}],
        )

    def test_remove_pending_registration_survives_null_email_row(self):
        # A stored ``{"email": null}`` row is a dict (so the isinstance filter
        # in ``get_pending_registrations`` keeps it) but the value is None. The
        # prior ``.get("email", "").lower()`` returned ``None`` — ``.get``'s
        # default only applies when the key is absent, not when the stored
        # value is None — and then crashed the filter with AttributeError,
        # taking out the /admin/registrations approve/reject POST via the
        # crash handler's empty 503.
        with patch.object(
            self.service,
            "get_pending_registrations",
            return_value=[
                {"email": None, "name": "corrupt"},
                {"email": "drop@example.com"},
                {"email": "keep@example.com"},
            ],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.remove_pending_registration("drop@example.com")
        save_json_mock.assert_called_once_with(
            self.config.registration_file,
            [
                {"email": None, "name": "corrupt"},
                {"email": "keep@example.com"},
            ],
        )

    def test_add_pending_lead_capture_appends_and_saves(self):
        with patch.object(
            self.service, "get_pending_lead_captures", return_value=[{"email": "keep@example.com"}]
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            result = self.service.add_pending_lead_capture(
                {"email": "new@example.com", "submitted_at": "2026-01-01"}
            )
        self.assertTrue(result)
        save_json_mock.assert_called_once_with(
            self.config.lead_capture_file,
            [
                {"email": "keep@example.com"},
                {"email": "new@example.com", "submitted_at": "2026-01-01"},
            ],
        )

    def test_add_pending_lead_capture_dedupes_existing_email(self):
        # Repeated submissions with the same email should not bloat the file,
        # and must report False so the route layer can skip the admin email
        # instead of replaying it on every duplicate POST.
        with patch.object(
            self.service, "get_pending_lead_captures", return_value=[{"email": "dup@example.com"}]
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            result = self.service.add_pending_lead_capture({"email": "dup@example.com"})
        self.assertFalse(result)
        save_json_mock.assert_not_called()

    def test_add_pending_lead_capture_ignores_missing_email(self):
        # Behaviour parity with ``SqlStorageService`` (see
        # ``test_add_pending_lead_capture_ignores_missing_email`` in
        # test_sql_storage.py): leads without an email are rejected so
        # they can't accumulate as anonymous rows the admin UI can never
        # associate with a real person.
        with patch.object(
            self.service, "get_pending_lead_captures", return_value=[]
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.assertFalse(
                self.service.add_pending_lead_capture({"submitted_at": "2026-01-01"})
            )
            self.assertFalse(self.service.add_pending_lead_capture({"email": ""}))
            self.assertFalse(self.service.add_pending_lead_capture({"email": None}))
        save_json_mock.assert_not_called()

    def test_remove_pending_lead_capture_filters_matching_email(self):
        with patch.object(
            self.service,
            "get_pending_lead_captures",
            return_value=[{"email": "keep@example.com"}, {"email": "drop@example.com"}],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.remove_pending_lead_capture("DROP@example.com")
        save_json_mock.assert_called_once_with(
            self.config.lead_capture_file,
            [{"email": "keep@example.com"}],
        )

    def test_add_pending_lead_capture_survives_null_email_row(self):
        # Same class of crash as
        # ``test_remove_pending_registration_survives_null_email_row``: a
        # ``{"email": null}`` row is a dict, slips past the isinstance filter,
        # then AttributeErrors inside the dedup ``any(...)`` walk because
        # ``.get("email", "")`` returned ``None`` and ``None.lower()`` raises.
        with patch.object(
            self.service,
            "get_pending_lead_captures",
            return_value=[{"email": None, "name": "corrupt"}],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.assertTrue(
                self.service.add_pending_lead_capture(
                    {"email": "new@example.com", "submitted_at": "2026-01-01"}
                )
            )
        save_json_mock.assert_called_once_with(
            self.config.lead_capture_file,
            [
                {"email": None, "name": "corrupt"},
                {"email": "new@example.com", "submitted_at": "2026-01-01"},
            ],
        )

    def test_remove_pending_lead_capture_survives_null_email_row(self):
        with patch.object(
            self.service,
            "get_pending_lead_captures",
            return_value=[
                {"email": None, "name": "corrupt"},
                {"email": "drop@example.com"},
                {"email": "keep@example.com"},
            ],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.remove_pending_lead_capture("DROP@example.com")
        save_json_mock.assert_called_once_with(
            self.config.lead_capture_file,
            [
                {"email": None, "name": "corrupt"},
                {"email": "keep@example.com"},
            ],
        )

    def test_get_pending_registrations_drops_non_dict_entries(self):
        # ``expected_type=list`` verifies the top-level container but not each
        # item's shape. A corrupted / hand-edited file that slips a bare
        # string, number, ``null``, or list into the array would otherwise
        # crash ``add_pending_registration``'s dedup check and the
        # ``admin_registrations`` route's iteration with AttributeError on
        # ``.get("email")``, and the crash handler serves the admin UI as an
        # empty 503. Matches the isinstance(dict) guard PR #144 added to
        # ``recent_listing_activity`` for the change-log JSONL.
        raw = [
            {"email": "keep@example.com"},
            "garbage",
            None,
            42,
            ["not", "a", "dict"],
            {"email": "also@example.com"},
        ]
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_pending_registrations()
        self.assertEqual(
            loaded,
            [{"email": "keep@example.com"}, {"email": "also@example.com"}],
        )

    def test_add_pending_registration_survives_non_dict_row_in_file(self):
        # Exercise the actual crash path: a stray non-dict row must not
        # AttributeError inside the dedup ``any(...)`` walk. Without the load
        # guard the whole call raises and the admin never sees the new row.
        with patch.object(
            self.service,
            "load_json_file",
            return_value=[
                {"email": "existing@example.com"},
                "corrupt",
                None,
            ],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.assertTrue(
                self.service.add_pending_registration({"email": "new@example.com"})
            )
        save_json_mock.assert_called_once_with(
            self.config.registration_file,
            [
                {"email": "existing@example.com"},
                {"email": "new@example.com"},
            ],
        )

    def test_get_pending_lead_captures_drops_non_dict_entries(self):
        # Same isinstance(dict) filter as pending registrations — protects the
        # dedup / remove paths and the admin UI from a hand-edited lead
        # capture file that carries a stray non-dict row.
        raw = [
            {"email": "keep@example.com"},
            "garbage",
            None,
            {"email": "also@example.com"},
        ]
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_pending_lead_captures()
        self.assertEqual(
            loaded,
            [{"email": "keep@example.com"}, {"email": "also@example.com"}],
        )

    def test_add_pending_lead_capture_survives_non_dict_row_in_file(self):
        # Exercise the actual crash path: a non-dict row must not
        # AttributeError inside the dedup ``any(...)`` walk.
        with patch.object(
            self.service,
            "load_json_file",
            return_value=[
                {"email": "existing@example.com"},
                42,
            ],
        ), patch.object(self.service, "save_json_file") as save_json_mock:
            self.assertTrue(
                self.service.add_pending_lead_capture(
                    {"email": "new@example.com", "submitted_at": "2026-01-01"}
                )
            )
        save_json_mock.assert_called_once_with(
            self.config.lead_capture_file,
            [
                {"email": "existing@example.com"},
                {"email": "new@example.com", "submitted_at": "2026-01-01"},
            ],
        )

    def test_get_renter_profiles_drops_non_dict_values(self):
        # ``expected_type=dict`` on load_json_file only verifies the top-level
        # container shape. A corrupted / hand-edited file that stores a
        # scalar or a list under an email key would otherwise reach the
        # ``renter_profile`` POST handler as ``profile["name"] = ...`` (which
        # TypeErrors on a string) or the ``ticket_routes._renter_email_default``
        # helper as ``profile.get(...)`` (which AttributeErrors on a list), and
        # the crash handler serves the page as an empty 503. Matches the
        # isinstance(dict) guards added for pending registrations, lead
        # captures, and tickets in PRs #146 / #147.
        raw = {
            "renter@example.com": {"name": "Renter R"},
            "corrupt-string@example.com": "not a dict",
            "corrupt-null@example.com": None,
            "corrupt-list@example.com": ["not", "a", "dict"],
            "renter2@example.com": {"name": "Renter Two"},
        }
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_renter_profiles()
        self.assertEqual(
            loaded,
            {
                "renter@example.com": {"name": "Renter R"},
                "renter2@example.com": {"name": "Renter Two"},
            },
        )

    def test_get_renter_contracts_drops_non_list_values_and_non_dict_items(self):
        # Same shape guard, one level deeper: ``expected_type=dict`` verifies
        # the outer email->contracts map but leaves the inner shape unchecked.
        # A hand-edited file that stores a scalar (instead of a list) under an
        # email key, or a list containing bare strings / nulls, would otherwise
        # crash ``admin_contracts`` (``setdefault(...).append`` on a non-list),
        # ``_backfill_contract_ids`` (``.get("id")`` on a non-dict), and the
        # CSV export (``.get(...)`` per row). The crash handler then serves
        # the admin UI as an empty 503.
        raw = {
            "renter@example.com": [
                {"id": "c1"},
                "not a dict",
                None,
                {"id": "c2"},
            ],
            "corrupt-scalar@example.com": "not a list",
            "corrupt-null@example.com": None,
            "renter2@example.com": [{"id": "c3"}],
        }
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_renter_contracts()
        self.assertEqual(
            loaded,
            {
                "renter@example.com": [{"id": "c1"}, {"id": "c2"}],
                "renter2@example.com": [{"id": "c3"}],
            },
        )

    def test_get_user_roles_drops_non_string_keys_and_values(self):
        # ``expected_type=dict`` on load_json_file only verifies the top-level
        # container. A corrupted / hand-edited ``user_roles.json`` can still
        # slip a non-string key (an integer, ``null``) or a non-string value
        # (a nested dict, a list, a bool) into the mapping. Every downstream
        # caller — ``AuthService.all_user_roles`` calling ``email.lower()``,
        # ``admin_dashboard_combined`` iterating ``.items()``, the /admin/users
        # role tally — would otherwise AttributeError / TypeError on the
        # non-string and take out the admin UI via the crash handler's
        # empty 503. Matches the isinstance guards PRs #146 / #147 / #148
        # added for pending registrations, tickets, and renter profiles.
        raw = {
            "admin@example.com": "admin",
            "renter@example.com": "renter",
            "corrupt-value-dict@example.com": {"role": "admin"},
            "corrupt-value-list@example.com": ["admin"],
            "corrupt-value-null@example.com": None,
            "corrupt-value-bool@example.com": True,
            123: "admin",
            None: "renter",
        }
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_user_roles()
        self.assertEqual(
            loaded,
            {
                "admin@example.com": "admin",
                "renter@example.com": "renter",
            },
        )

    def test_get_hidden_listing_ids_drops_non_string_and_empty_entries(self):
        # ``expected_type=list`` verifies the outer container but not each
        # entry. A hand-edited / corrupted ``hidden_listings.json`` can slip
        # a ``null``, a bare number, a nested dict, or an empty string into
        # the array. The previous ``[str(item) for item in ids]`` turned
        # those into "None", "42", "{}" — junk strings that never match a
        # real property id but still sit in the hidden set forever (via
        # ``PropertyService.hidden_listing_ids`` wrapping the return value
        # in ``set(...)``). An empty string is worse: nothing legitimately
        # has id "", so it's noise, but if a property ever normalized to
        # id "" it would be silently hidden. Matches the isinstance guards
        # PRs #144-#153 added on the rest of the storage surface.
        raw = [
            "prop-1",
            None,
            42,
            {"nested": "dict"},
            "",
            "prop-2",
        ]
        with patch.object(self.service, "load_json_file", return_value=raw):
            loaded = self.service.get_hidden_listing_ids()
        self.assertEqual(loaded, ["prop-1", "prop-2"])

    def test_set_user_role_lowercases_email_and_saves(self):
        with patch.object(self.service, "get_user_roles", return_value={}), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            self.service.set_user_role("Admin@Example.com", "admin")

        save_json_mock.assert_called_once_with(
            self.config.user_roles_file,
            {"admin@example.com": "admin"},
        )

    def test_delete_user_role_returns_true_when_removed(self):
        with patch.object(self.service, "get_user_roles", return_value={"admin@example.com": "admin"}), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            removed = self.service.delete_user_role("admin@example.com")

        self.assertTrue(removed)
        save_json_mock.assert_called_once_with(self.config.user_roles_file, {"admin@example.com": "revoked"})

    def test_delete_user_role_returns_false_when_missing(self):
        with patch.object(self.service, "get_user_roles", return_value={}), patch.object(
            self.service,
            "save_json_file",
        ) as save_json_mock:
            removed = self.service.delete_user_role("missing@example.com")

        self.assertFalse(removed)
        save_json_mock.assert_called_once_with(self.config.user_roles_file, {"missing@example.com": "revoked"})

    def test_save_renter_profiles_delegates_to_save_json_file(self):
        profiles = {"renter@example.com": {"name": "Jamie"}}
        with patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.save_renter_profiles(profiles)

        save_json_mock.assert_called_once_with(self.config.renter_profile_file, profiles)

    def test_save_renter_contracts_delegates_to_save_json_file(self):
        contracts = {"renter@example.com": [{"property_name": "Maple House"}]}
        with patch.object(self.service, "save_json_file") as save_json_mock:
            self.service.save_renter_contracts(contracts)

        save_json_mock.assert_called_once_with(self.config.contracts_file, contracts)

    def test_concurrent_add_pending_registration_preserves_all_writes(self):
        """Lost-update race fix: load+save runs under the file lock.

        Without holding ``file_lock`` across get_pending_registrations() and
        save_json_file(), N concurrent submissions all load the pre-write
        list, then race each other's writes — empirically ~80% of entries
        get silently dropped (one run with N=50 kept only 8 on disk). The
        in-process Flask dev server threads each request, so this race is
        reachable on the live site, not a theoretical concern.
        """
        import threading
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = SimpleNamespace(
            registration_file=Path(tmpdir.name) / "registrations.json",
            user_roles_file=Path(tmpdir.name) / "roles.json",
            renter_profile_file=Path(tmpdir.name) / "profiles.json",
            contracts_file=Path(tmpdir.name) / "contracts.json",
            lead_capture_file=Path(tmpdir.name) / "lead_captures.json",
        )
        service = FileStorageService(config)

        N = 32
        barrier = threading.Barrier(N)
        successes: list[bool] = []
        successes_lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait()
            ok = service.add_pending_registration({
                "email": f"user{i}@example.com",
                "name": f"User {i}",
                "reason": "race",
            })
            with successes_lock:
                successes.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = service.get_pending_registrations()
        self.assertEqual(sum(successes), N)
        self.assertEqual(len(stored), N)
        stored_emails = {item.get("email") for item in stored}
        self.assertEqual(
            stored_emails,
            {f"user{i}@example.com" for i in range(N)},
        )

    def test_atomic_serializes_concurrent_renter_contracts_updates(self):
        """Route-level read-modify-write under ``atomic()`` preserves all writes.

        ``admin_contracts`` (add/delete) and ``_backfill_contract_ids`` each
        do ``get_renter_contracts() -> mutate -> save_renter_contracts()``
        without holding the storage lock, so two concurrent admins racing
        on the contracts file silently dropped one side's update — same
        lost-update class commit e062313 fixed for tickets and pending
        registrations. The route handlers now wrap that sequence in
        ``with services.storage.atomic():``; this regression test verifies
        the lock is actually held end-to-end on the file backend.
        """
        import threading
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = SimpleNamespace(
            registration_file=Path(tmpdir.name) / "registrations.json",
            user_roles_file=Path(tmpdir.name) / "roles.json",
            renter_profile_file=Path(tmpdir.name) / "profiles.json",
            contracts_file=Path(tmpdir.name) / "contracts.json",
            lead_capture_file=Path(tmpdir.name) / "lead_captures.json",
        )
        service = FileStorageService(config)

        N = 32
        barrier = threading.Barrier(N)

        def worker(i: int) -> None:
            barrier.wait()
            # Mirror what admin_contracts add now does after the fix.
            with service.atomic():
                contracts = service.get_renter_contracts()
                contracts.setdefault(f"renter{i}@example.com", []).append(
                    {"id": f"c{i}", "property_name": f"P{i}"}
                )
                service.save_renter_contracts(contracts)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = service.get_renter_contracts()
        self.assertEqual(len(stored), N)
        self.assertEqual(
            {email for email in stored},
            {f"renter{i}@example.com" for i in range(N)},
        )

    def test_atomic_serializes_concurrent_renter_profile_updates(self):
        """``renter_profile`` POST now wraps load+modify+save in ``atomic()``.

        Without the wrap, two renters POSTing to /renter/profile at the
        same time both load the global profiles dict, each modifies their
        own row, and the second save clobbers the first — even though
        they're editing different rows — because the file IS the dict.
        """
        import threading
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config = SimpleNamespace(
            registration_file=Path(tmpdir.name) / "registrations.json",
            user_roles_file=Path(tmpdir.name) / "roles.json",
            renter_profile_file=Path(tmpdir.name) / "profiles.json",
            contracts_file=Path(tmpdir.name) / "contracts.json",
            lead_capture_file=Path(tmpdir.name) / "lead_captures.json",
        )
        service = FileStorageService(config)

        N = 32
        barrier = threading.Barrier(N)

        def worker(i: int) -> None:
            barrier.wait()
            # Mirror what renter_profile POST now does after the fix.
            with service.atomic():
                profiles = service.get_renter_profiles()
                profiles[f"renter{i}@example.com"] = {"name": f"Renter {i}"}
                service.save_renter_profiles(profiles)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = service.get_renter_profiles()
        self.assertEqual(len(stored), N)
        self.assertEqual(
            set(stored),
            {f"renter{i}@example.com" for i in range(N)},
        )


class AppointmentServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.appointments_path = Path(self.tmpdir.name) / "appointments.txt"
        self.config = SimpleNamespace(property_appointments_file=self.appointments_path)
        self.service = AppointmentService(self.config)

    def test_load_returns_empty_when_file_missing(self):
        self.assertEqual(self.service.load(), {})

    def test_save_and_load_round_trip(self):
        self.service.save({"prop-1": {"2030-01-11", "2030-01-10"}, "prop-2": {"2030-02-01"}})
        loaded = self.service.load()
        self.assertEqual(loaded["prop-1"], {"2030-01-10", "2030-01-11"})
        self.assertEqual(loaded["prop-2"], {"2030-02-01"})
        # Dates should be persisted in sorted order on disk.
        on_disk = self.appointments_path.read_text(encoding="utf-8")
        self.assertIn("prop-1:2030-01-10,2030-01-11", on_disk)

    def test_save_is_atomic_no_temp_files_left_behind(self):
        self.service.save({"prop-1": {"2030-01-10"}})
        leftovers = [p for p in self.appointments_path.parent.iterdir() if p.name != "appointments.txt"]
        self.assertEqual(leftovers, [], f"Unexpected temp files: {leftovers}")

    def test_save_failure_preserves_existing_file(self):
        # First save establishes the original contents we want preserved.
        self.service.save({"prop-1": {"2030-01-10"}})
        original = self.appointments_path.read_text(encoding="utf-8")

        # Force the os.replace step to fail; the original file must survive
        # untouched and no leftover temp file may remain.
        with patch("somewheria_app.services.appointments.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.save({"prop-1": {"2099-12-31"}})

        self.assertEqual(self.appointments_path.read_text(encoding="utf-8"), original)
        leftovers = [p for p in self.appointments_path.parent.iterdir() if p.name != "appointments.txt"]
        self.assertEqual(leftovers, [], f"Temp file leaked after failed save: {leftovers}")

    def test_load_ignores_malformed_lines(self):
        self.appointments_path.write_text(
            "prop-1:2030-01-10,2030-01-11\nmalformed\nprop-2:2030-02-01\n",
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(loaded["prop-1"], {"2030-01-10", "2030-01-11"})
        self.assertEqual(loaded["prop-2"], {"2030-02-01"})

    def test_book_persists_new_appointment(self):
        self.assertTrue(self.service.book("prop-1", "2030-05-01"))
        loaded = self.service.load()
        self.assertEqual(loaded["prop-1"], {"2030-05-01"})

    def test_book_rejects_double_booking(self):
        self.assertTrue(self.service.book("prop-1", "2030-05-01"))
        self.assertFalse(self.service.book("prop-1", "2030-05-01"))
        loaded = self.service.load()
        self.assertEqual(loaded["prop-1"], {"2030-05-01"})

    def test_book_accumulates_distinct_dates(self):
        self.assertTrue(self.service.book("prop-1", "2030-05-01"))
        self.assertTrue(self.service.book("prop-1", "2030-05-02"))
        self.assertTrue(self.service.book("prop-2", "2030-05-01"))
        loaded = self.service.load()
        self.assertEqual(loaded["prop-1"], {"2030-05-01", "2030-05-02"})
        self.assertEqual(loaded["prop-2"], {"2030-05-01"})

    def test_load_skips_empty_property_id_line(self):
        # A line stored with an empty property id (``:2030-01-10``) would
        # otherwise land in the returned map under the "" key and get
        # re-written verbatim by the next save(). Skip it silently so a
        # hand-edited or corrupted file doesn't accumulate garbage entries.
        self.appointments_path.write_text(
            "prop-1:2030-01-10\n:2030-01-11\n",
            encoding="utf-8",
        )
        loaded = self.service.load()
        self.assertEqual(loaded, {"prop-1": {"2030-01-10"}})
        self.assertNotIn("", loaded)

    def test_load_does_not_log_at_info_on_every_call(self):
        # load() is called on every /property/<uuid> page render; routine
        # traces must sit at DEBUG so real-user traffic doesn't dominate
        # application.log with no-signal messages. Regressions here would
        # re-introduce the log flood this cleanup addressed.
        self.appointments_path.write_text("prop-1:2030-01-10\n", encoding="utf-8")
        with patch.object(self.service.logger, "info") as info_mock:
            self.service.load()
            # Also cover the "missing file" branch — same rationale.
            self.appointments_path.unlink()
            self.service.load()
        info_mock.assert_not_called()

    def test_save_does_not_log_at_info_or_call_print_check_file(self):
        # save() is called from every booking; the successful path is
        # observable via os.replace(), and log_site_change / the notification
        # email in schedule_appointment already record the business event.
        # An INFO trace here (and the redundant print_check_file() call the
        # previous implementation made) is dead weight.
        with patch.object(self.service.logger, "info") as info_mock, patch.object(
            self.service, "print_check_file"
        ) as print_check_mock:
            self.service.save({"prop-1": {"2030-05-01"}})
        info_mock.assert_not_called()
        print_check_mock.assert_not_called()


class PropertyServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.notifications = Mock()
        self.config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        self.service = PropertyService(self.config, self.notifications)

    def test_serialize_properties_converts_sets_to_lists(self):
        serialized = self.service.serialize_properties(
            [{"id": "prop-1", "amenities": {"Parking", "Laundry"}, "status": "Active"}]
        )

        self.assertCountEqual(serialized[0]["amenities"], ["Parking", "Laundry"])

    def test_property_count_returns_length_without_deep_copy(self):
        # property_count() is the cheap alternative to
        # ``len(get_cached_properties())`` — it must return the same length
        # without deep-copying the underlying cache entries, which would
        # otherwise allocate a fresh list and dict tree per admin dashboard
        # render just to compute a size.
        sentinel = {"id": "prop-1", "photos": ["data:image/jpeg;base64,AAAA"]}
        self.service.cache = [sentinel, {"id": "prop-2"}]

        self.assertEqual(self.service.property_count(), 2)
        # The exact object must still be inside the cache — property_count
        # must not have copied it.
        self.assertIs(self.service.cache[0], sentinel)

    def test_property_count_empty_cache(self):
        self.service.cache = []
        self.assertEqual(self.service.property_count(), 0)

    def test_normalize_property_applies_defaults(self):
        normalized = self.service.normalize_property({"name": "Maple House"}, "prop-1")

        self.assertEqual(normalized["id"], "prop-1")
        self.assertEqual(normalized["bedrooms"], "N/A")
        self.assertEqual(normalized["pets_allowed"], "Unknown")
        self.assertEqual(normalized["ada_accessible"], "Unknown")

    def test_normalize_property_converts_boolean_flags(self):
        normalized = self.service.normalize_property(
            {"name": "Maple House", "pets_allowed": True, "ada_accessible": False},
            "prop-1",
        )

        self.assertEqual(normalized["pets_allowed"], "Yes")
        self.assertEqual(normalized["ada_accessible"], "No")

    def test_normalize_property_converts_string_accessibility_flags(self):
        normalized = self.service.normalize_property({"accessible": "yes"}, "prop-1")

        self.assertEqual(normalized["ada_accessible"], "Yes")

    def test_normalize_property_rejects_unknown_accessibility_values(self):
        normalized = self.service.normalize_property({"accessible": "maybe"}, "prop-1")

        self.assertEqual(normalized["ada_accessible"], "Unknown")

    def test_normalize_property_uses_photo_as_thumbnail_when_missing(self):
        normalized = self.service.normalize_property({"photos": ["photo-1.jpg"]}, "prop-1")

        self.assertEqual(normalized["thumbnail"], "photo-1.jpg")

    def test_normalize_property_coerces_null_description_to_empty_string(self):
        # Upstream sometimes returns ``"description": null`` for partially
        # filled listings. Previously this crashed in the pets-inference branch
        # (``description.lower()`` on None) — fetch_property_record swallowed the
        # exception and the property silently dropped out of the listing.
        normalized = self.service.normalize_property(
            {"name": "Maple", "description": None}, "prop-1"
        )

        self.assertEqual(normalized["description"], "")
        self.assertEqual(normalized["blurb"], "")
        self.assertEqual(normalized["pets_allowed"], "Unknown")

    def test_normalize_property_coerces_non_string_description_to_empty_string(self):
        normalized = self.service.normalize_property(
            {"name": "Maple", "description": 42}, "prop-1"
        )

        self.assertEqual(normalized["description"], "")
        self.assertEqual(normalized["blurb"], "")

    def test_normalize_property_coerces_null_included_amenities_to_empty_list(self):
        # Same upstream-shape bug as the description coercion above: a
        # ``"included_amenities": null`` payload made the pets-inference branch
        # iterate over None and raise TypeError, which fetch_property_record
        # swallowed as a generic failure so the property dropped silently from
        # the listing.
        normalized = self.service.normalize_property(
            {"name": "Maple", "included_amenities": None}, "prop-1"
        )

        self.assertEqual(normalized["included_amenities"], [])
        self.assertEqual(normalized["pets_allowed"], "Unknown")

    def test_normalize_property_coerces_null_included_utilities_fallback_to_empty_list(self):
        # When ``included_amenities`` is absent and the legacy
        # ``included_utilities`` key is None, setdefault propagates the None
        # — same crash, same disappear-from-listing symptom.
        normalized = self.service.normalize_property(
            {"name": "Maple", "included_utilities": None}, "prop-1"
        )

        self.assertEqual(normalized["included_amenities"], [])

    def test_normalize_property_infers_pets_from_description_when_amenities_null(self):
        # The null-amenities guard must not block the description fallback
        # for pet inference.
        normalized = self.service.normalize_property(
            {
                "name": "Maple",
                "included_amenities": None,
                "description": "Dog-friendly home; pet deposit required.",
            },
            "prop-1",
        )

        self.assertEqual(normalized["included_amenities"], [])
        self.assertEqual(normalized["pets_allowed"], "Yes")

    def test_normalize_property_coerces_non_list_included_amenities_to_empty_list(self):
        # A malformed upstream payload that returns a string (or any other
        # non-list) for included_amenities must not crash; the string would
        # iterate per-character and silently misclassify the pets flag.
        normalized = self.service.normalize_property(
            {"name": "Maple", "included_amenities": "Parking, Laundry"}, "prop-1"
        )

        self.assertEqual(normalized["included_amenities"], [])

    def test_normalize_property_coerces_null_scalars_to_defaults(self):
        # Same upstream-shape bug as the description / included_amenities
        # null fixes: a partially-filled listing can return ``{"name": null,
        # "address": null, "rent": null, ...}``. ``setdefault`` leaves None
        # in place, so templates / page titles render the literal "None".
        # The coercion path here must replace each scalar with its safe
        # default so the listing is presentable rather than ugly.
        normalized = self.service.normalize_property(
            {
                "name": None,
                "address": None,
                "rent": None,
                "deposit": None,
                "sqft": None,
                "bedrooms": None,
                "bathrooms": None,
                "lease_length": None,
                "thumbnail": None,
            },
            "prop-1",
        )

        self.assertEqual(normalized["name"], "Property")
        self.assertEqual(normalized["address"], "N/A")
        self.assertEqual(normalized["rent"], "N/A")
        self.assertEqual(normalized["deposit"], "N/A")
        self.assertEqual(normalized["sqft"], "N/A")
        self.assertEqual(normalized["bedrooms"], "N/A")
        self.assertEqual(normalized["bathrooms"], "N/A")
        self.assertEqual(normalized["lease_length"], "12 months")
        # Thumbnail falls back to the first photo, or "" when no photos exist.
        self.assertEqual(normalized["thumbnail"], "")

    def test_normalize_property_null_thumbnail_falls_back_to_first_photo(self):
        # If the upstream payload explicitly nulls ``thumbnail`` but does
        # ship a photo list, prefer the first photo over leaving None on the
        # record (which would render as <img src="None">).
        normalized = self.service.normalize_property(
            {"name": "Maple", "thumbnail": None, "photos": ["photo-1.jpg"]},
            "prop-1",
        )

        self.assertEqual(normalized["thumbnail"], "photo-1.jpg")

    def test_normalize_property_coerces_null_blurb_to_description(self):
        # blurb mirrors description on the listing card; a null upstream
        # value previously slipped through to the template and rendered as
        # the string "None".
        normalized = self.service.normalize_property(
            {"name": "Maple", "description": "Cozy duplex.", "blurb": None},
            "prop-1",
        )

        self.assertEqual(normalized["blurb"], "Cozy duplex.")

    def test_property_payload_from_form_merges_custom_amenities(self):
        form = DummyForm(
            values={
                "name": "Maple House",
                "pets_allowed": "Yes",
                "custom_amenities": "Garden, Storage ",
            },
            lists={"amenities": ["Parking", "Laundry"]},
        )

        payload = self.service.property_payload_from_form(form)

        self.assertEqual(payload["pets_allowed"], "Yes")
        # "Laundry" is an alias of the "Washer/Dryer" checkbox label and is
        # canonicalized at write time so edit-save round-trips can't stack
        # duplicate variants of the same amenity.
        self.assertEqual(payload["included_amenities"], ["Parking", "Washer/Dryer", "Garden", "Storage"])

    def test_set_listing_active_false_hides_and_logs(self):
        self.service.cache = [{"id": "prop-1"}]
        storage = Mock()
        self.service.storage = storage

        self.service.set_listing_active("prop-1", active=False, actor_email="admin@example.com")

        storage.set_listing_hidden.assert_called_once_with("prop-1", True)
        self.notifications.log_site_change.assert_called_once_with(
            "admin@example.com",
            "property_deactivated",
            {"property_id": "prop-1"},
        )

    def test_set_listing_active_true_unhides_and_logs(self):
        self.service.cache = [{"id": "prop-1"}]
        storage = Mock()
        self.service.storage = storage

        self.service.set_listing_active("prop-1", active=True, actor_email="admin@example.com")

        storage.set_listing_hidden.assert_called_once_with("prop-1", False)
        self.notifications.log_site_change.assert_called_once_with(
            "admin@example.com",
            "property_reactivated",
            {"property_id": "prop-1"},
        )

    def test_set_listing_active_missing_property_raises(self):
        self.service.cache = []
        self.service.storage = Mock()

        with self.assertRaises(KeyError):
            self.service.set_listing_active("ghost", active=False, actor_email="admin@example.com")

    def test_get_visible_properties_filters_hidden_ids(self):
        self.service.cache = [{"id": "prop-1"}, {"id": "prop-2"}]
        storage = Mock()
        storage.get_hidden_listing_ids.return_value = ["prop-1"]
        self.service.storage = storage

        visible = self.service.get_visible_properties()

        self.assertEqual([item["id"] for item in visible], ["prop-2"])

    def test_get_visible_properties_without_storage_returns_all(self):
        self.service.cache = [{"id": "prop-1"}, {"id": "prop-2"}]
        self.service.storage = None

        visible = self.service.get_visible_properties()

        self.assertEqual([item["id"] for item in visible], ["prop-1", "prop-2"])

    def test_delete_property_updates_cache_and_logs_change(self):
        self.service.cache = [{"id": "prop-1"}, {"id": "prop-2"}]
        response = Mock(status_code=204, text="")
        with patch("somewheria_app.services.properties.requests.delete", return_value=response) as delete_mock:
            self.service.delete_property("prop-1", "admin@example.com")

        self.assertEqual(self.service.cache, [{"id": "prop-2"}])
        delete_mock.assert_called_once()
        self.notifications.log_site_change.assert_called_once_with(
            "admin@example.com",
            "property_deleted",
            {"property_id": "prop-1"},
        )

    def test_delete_property_raises_on_remote_error(self):
        self.service.cache = [{"id": "prop-1"}]
        response = Mock(status_code=500, text="boom")
        with patch("somewheria_app.services.properties.requests.delete", return_value=response):
            with self.assertRaises(RuntimeError):
                self.service.delete_property("prop-1", "admin@example.com")

    def test_delete_property_clears_hidden_listing_tombstone(self):
        # Regression: an admin who deactivates (hides) a listing and then
        # deletes it upstream previously left the id in hidden_listings
        # forever. That set is materialized on every /for-rent render, and
        # if the upstream ever re-issues the same UUID the "new" listing
        # would be silently hidden from the public site.
        self.service.cache = [{"id": "prop-1"}]
        storage = Mock()
        self.service.storage = storage
        response = Mock(status_code=204, text="")
        with patch("somewheria_app.services.properties.requests.delete", return_value=response):
            self.service.delete_property("prop-1", "admin@example.com")

        storage.set_listing_hidden.assert_called_once_with("prop-1", False)

    def test_delete_property_without_storage_does_not_crash(self):
        # ``storage`` is optional on PropertyService — the cleanup call must
        # be guarded so a service configured without a storage backend still
        # deletes correctly.
        self.service.cache = [{"id": "prop-1"}]
        self.service.storage = None
        response = Mock(status_code=204, text="")
        with patch("somewheria_app.services.properties.requests.delete", return_value=response):
            self.service.delete_property("prop-1", "admin@example.com")

        self.assertEqual(self.service.cache, [])

    def test_delete_property_rejects_invalid_id_before_outbound_call(self):
        # A traversal-style id must be rejected at the boundary so a
        # malformed value can't be smuggled into the outbound DELETE URL.
        # KeyError matches the "not found" semantics the route handler maps
        # to a 404 response.
        self.service.cache = [{"id": "prop-1"}]
        with patch("somewheria_app.services.properties.requests.delete") as delete_mock:
            with self.assertRaises(KeyError):
                self.service.delete_property("../../etc/passwd", "admin@example.com")

        delete_mock.assert_not_called()
        # Cache must be untouched when validation fails.
        self.assertEqual(self.service.cache, [{"id": "prop-1"}])
        self.notifications.log_site_change.assert_not_called()

    def test_toggle_sale_updates_cache_and_status(self):
        self.service.cache = [{"id": "prop-1", "for_sale": False, "status": "Active"}]
        with patch("somewheria_app.services.properties.requests.put") as put_mock:
            self.service.toggle_sale("prop-1", "admin@example.com")

        self.assertTrue(self.service.cache[0]["for_sale"])
        self.assertEqual(self.service.cache[0]["status"], "For Sale")
        put_mock.assert_called_once()
        self.notifications.log_site_change.assert_called_once_with(
            "admin@example.com",
            "property_toggle_sale",
            {"property_id": "prop-1", "for_sale": True},
        )

    def test_toggle_sale_raises_when_property_missing(self):
        self.service.cache = []

        with self.assertRaises(KeyError):
            self.service.toggle_sale("missing", "admin@example.com")

    def test_fetch_live_property_name_returns_name(self):
        response = Mock()
        response.json.return_value = {"name": "Maple House"}
        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            name = self.service.fetch_live_property_name("prop-1")

        self.assertEqual(name, "Maple House")

    def test_fetch_live_property_name_returns_none_on_error(self):
        with patch("somewheria_app.services.properties.requests.get", side_effect=RuntimeError("boom")):
            name = self.service.fetch_live_property_name("prop-1")

        self.assertIsNone(name)

    def test_fetch_live_property_name_returns_none_for_invalid_id(self):
        # A traversal-style id must be rejected at the boundary so the
        # outbound URL can never reach an unintended upstream path.
        with patch("somewheria_app.services.properties.requests.get") as get_mock:
            name = self.service.fetch_live_property_name("../../etc/passwd")

        self.assertIsNone(name)
        get_mock.assert_not_called()

    def test_fetch_live_property_name_returns_none_on_http_error_status(self):
        # A 404/5xx with a JSON error body must not be treated as a real
        # property name. Without raise_for_status() this would have leaked
        # through and the caller would have proceeded as if the property
        # existed.
        from requests import HTTPError

        response = Mock()
        response.raise_for_status.side_effect = HTTPError("404")
        response.json.return_value = {"name": "Should Be Ignored"}
        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            name = self.service.fetch_live_property_name("prop-1")

        self.assertIsNone(name)

    def test_fetch_live_property_name_returns_none_when_name_missing(self):
        # Upstream answered 200 but the JSON has no usable name; we must
        # signal "not found" rather than fall back to a generic placeholder
        # that callers would treat as a successful lookup.
        response = Mock()
        response.json.return_value = {"address": "123 Main"}
        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            name = self.service.fetch_live_property_name("prop-1")

        self.assertIsNone(name)

    def test_fetch_live_property_name_returns_none_when_payload_not_a_dict(self):
        response = Mock()
        response.json.return_value = ["unexpected", "list"]
        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            name = self.service.fetch_live_property_name("prop-1")

        self.assertIsNone(name)

    def test_fetch_property_record_returns_none_on_http_error(self):
        # Upstream returns valid JSON but with a 5xx status code. Without
        # raise_for_status() the error body would be passed through as a
        # property record.
        from requests import HTTPError

        details_response = Mock()
        details_response.raise_for_status.side_effect = HTTPError("500")
        details_response.json.return_value = {"error": "boom"}

        with patch(
            "somewheria_app.services.properties.requests.get",
            return_value=details_response,
        ):
            self.assertIsNone(self.service.fetch_property_record("prop-1"))

    def test_fetch_property_record_skips_non_dict_payload(self):
        details_response = Mock()
        details_response.raise_for_status.return_value = None
        details_response.json.return_value = ["not", "a", "dict"]

        with patch(
            "somewheria_app.services.properties.requests.get",
            return_value=details_response,
        ):
            self.assertIsNone(self.service.fetch_property_record("prop-1"))

    def test_get_base64_image_from_url_rejects_oversize_content_length(self):
        from somewheria_app.services.properties import MAX_IMAGE_BYTES

        oversize = MAX_IMAGE_BYTES + 1
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(oversize)}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = iter([])

        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            self.assertIsNone(self.service.get_base64_image_from_url("https://example.com/big.jpg"))

    def test_get_base64_image_from_url_rejects_oversize_streamed(self):
        from somewheria_app.services.properties import MAX_IMAGE_BYTES

        # No Content-Length header (chunked / unknown size); the cap must be
        # enforced while iterating chunks.
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = iter([b"x" * (MAX_IMAGE_BYTES + 1)])

        with patch("somewheria_app.services.properties.requests.get", return_value=response):
            self.assertIsNone(self.service.get_base64_image_from_url("https://example.com/big.jpg"))


class NotificationServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.analytics = Mock()
        self.config = SimpleNamespace(
            email_sender="sender@example.com",
            email_recipient="recipient@example.com",
            log_file=Path("application.log"),
            change_log_file=Path("site_changes.log"),
        )
        self.service = NotificationService(self.config, self.analytics)

    def test_send_email_returns_false_when_password_missing(self):
        with patch.object(self.service, "_email_password", return_value=""):
            result = self.service.send_email("Test Subject", "Hello world")

        self.assertFalse(result)

    def test_send_email_returns_true_on_success(self):
        smtp_instance = Mock()
        smtp_context = Mock()
        smtp_context.__enter__ = Mock(return_value=smtp_instance)
        smtp_context.__exit__ = Mock(return_value=None)

        with patch.object(self.service, "_email_password", return_value="app-pass"), patch(
            "somewheria_app.services.notifications.smtplib.SMTP", return_value=smtp_context
        ) as smtp_ctor:
            result = self.service.send_email("Test Subject", "Hello world")

        self.assertTrue(result)
        # The SMTP constructor MUST be called with a timeout so a slow / hung
        # Gmail relay cannot block the calling thread forever — crash-handler
        # emails run in daemon threads and would otherwise pile up.
        ctor_kwargs = smtp_ctor.call_args.kwargs
        ctor_args = smtp_ctor.call_args.args
        self.assertEqual(ctor_args[0], "smtp.gmail.com")
        self.assertEqual(ctor_args[1], 587)
        self.assertIn("timeout", ctor_kwargs)
        self.assertGreater(ctor_kwargs["timeout"], 0)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("sender@example.com", "app-pass")
        smtp_instance.send_message.assert_called_once()
        sent_message = smtp_instance.send_message.call_args.args[0]
        self.assertTrue(sent_message.is_multipart())
        html_part = sent_message.get_body(preferencelist=("html",))
        text_part = sent_message.get_body(preferencelist=("plain",))
        self.assertIsNotNone(html_part)
        self.assertIsNotNone(text_part)
        self.assertIn("Somewheria LLC", html_part.get_content())

    def test_send_email_returns_false_on_smtp_failure(self):
        smtp_instance = Mock()
        smtp_instance.send_message.side_effect = RuntimeError("smtp boom")
        smtp_context = Mock()
        smtp_context.__enter__ = Mock(return_value=smtp_instance)
        smtp_context.__exit__ = Mock(return_value=None)

        with patch.object(self.service, "_email_password", return_value="app-pass"), patch(
            "somewheria_app.services.notifications.smtplib.SMTP", return_value=smtp_context
        ):
            result = self.service.send_email("Test Subject", "Hello world")

        self.assertFalse(result)

    def test_send_email_sanitizes_newlines_in_subject(self):
        # ``EmailMessage`` raises ValueError when a header value carries a CR
        # or LF (its header-injection guard). Ticket titles ride into the
        # Subject verbatim, so a title with a stray newline used to turn
        # every downstream send into a swallowed failure. The sanitizer
        # collapses CR/LF/NUL to spaces so the send still goes out, and the
        # relay sees a single-line Subject.
        smtp_instance = Mock()
        smtp_context = Mock()
        smtp_context.__enter__ = Mock(return_value=smtp_instance)
        smtp_context.__exit__ = Mock(return_value=None)

        with patch.object(self.service, "_email_password", return_value="app-pass"), patch(
            "somewheria_app.services.notifications.smtplib.SMTP", return_value=smtp_context
        ):
            result = self.service.send_email(
                "New Repair Ticket: leak\r\nBcc: attacker@evil.com",
                "body",
            )

        self.assertTrue(result)
        smtp_instance.send_message.assert_called_once()
        sent_message = smtp_instance.send_message.call_args.args[0]
        subject = sent_message["Subject"]
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)
        # The visible text is preserved so operators still see the title.
        self.assertIn("New Repair Ticket: leak", subject)
        self.assertIn("Bcc: attacker@evil.com", subject)

    def test_send_email_truncates_absurdly_long_subject(self):
        # A runaway subject (accidental paste, hostile POST) must not sail
        # through to the relay unbounded. Truncate at the sanitizer so both
        # the outbound header and the HTML preview stay a reasonable size.
        smtp_instance = Mock()
        smtp_context = Mock()
        smtp_context.__enter__ = Mock(return_value=smtp_instance)
        smtp_context.__exit__ = Mock(return_value=None)

        long_subject = "x" * 5000
        with patch.object(self.service, "_email_password", return_value="app-pass"), patch(
            "somewheria_app.services.notifications.smtplib.SMTP", return_value=smtp_context
        ):
            self.service.send_email(long_subject, "body")

        sent_message = smtp_instance.send_message.call_args.args[0]
        subject = sent_message["Subject"]
        # 200-char ceiling from _SUBJECT_MAX_LEN, so total <= 200.
        self.assertLessEqual(len(subject), 200)

    def test_html_email_body_formats_subject_and_lines(self):
        html_body = self.service._html_email_body(
            "Image Edited Notification",
            "The following image(s) have been edited:\nhttps://example.com/a.jpg",
        )

        self.assertIn("Image Edited Notification", html_body)
        self.assertIn("Somewheria LLC", html_body)
        self.assertIn("https://example.com/a.jpg", html_body)

    def test_log_and_notify_error_records_error_and_dispatches_async(self):
        # log_and_notify_error must NOT block the caller on SMTP delivery — a
        # slow Gmail relay would otherwise add up to SMTP_TIMEOUT_SECONDS (30 s)
        # of latency to whichever request thread triggered the error. Assert
        # the dispatch happens through send_email_async so the request-hot
        # callsites (admin_routes, auth_routes, public_routes, ticket_routes,
        # properties.upload_image) stay snappy.
        with patch.object(self.service, "send_email_async") as async_mock:
            self.service.log_and_notify_error("Save Error", "Something broke")

        self.analytics.record_error.assert_called_once()
        async_mock.assert_called_once_with("Save Error", "Something broke")

    def test_log_and_notify_error_delivery_matches_disable_background_threads(self):
        # Under DISABLE_BACKGROUND_THREADS=1 the async wrapper runs send_email
        # inline in the caller's thread. This preserves the invariant tests
        # rely on: mocking send_email is still enough to observe (or suppress)
        # error-notification email delivery.
        with patch.dict(os.environ, {"DISABLE_BACKGROUND_THREADS": "1"}), patch.object(
            self.service, "send_email"
        ) as send_email_mock:
            self.service.log_and_notify_error("Save Error", "Something broke")

        send_email_mock.assert_called_once_with("Save Error", "Something broke", to=None)

    def test_log_and_notify_error_suppresses_repeat_email_within_cooldown(self):
        # A sustained failure (Google OAuth outage, spam scan of the callback,
        # a repeatedly-failing property save) MUST NOT mail-bomb the admin
        # inbox or fan out a fresh 30s-timeout daemon thread per event. Only
        # the SMTP send is suppressed on repeats; analytics + console still
        # record every event so the incident is observable.
        with patch.object(self.service, "send_email_async") as async_mock:
            self.service.log_and_notify_error("Google OAuth Error", "invalid_grant")
            self.service.log_and_notify_error("Google OAuth Error", "invalid_grant again")
            self.service.log_and_notify_error("Google OAuth Error", "still broken")

        # Analytics + console record every event; only the email is throttled.
        self.assertEqual(self.analytics.record_error.call_count, 3)
        async_mock.assert_called_once_with("Google OAuth Error", "invalid_grant")

    def test_log_and_notify_error_different_subjects_send_independently(self):
        # Suppression is per-subject: an OAuth outage should not mask a
        # separate save-edit failure that happens moments later.
        with patch.object(self.service, "send_email_async") as async_mock:
            self.service.log_and_notify_error("Google OAuth Error", "boom")
            self.service.log_and_notify_error("Save Edit Error", "different boom")

        self.assertEqual(async_mock.call_count, 2)

    def test_log_and_notify_error_dispatches_again_after_cooldown_elapses(self):
        # Once the cooldown window passes, the next event dispatches again.
        # Advance ``time.monotonic`` past the cooldown rather than sleeping
        # so the test stays fast and deterministic.
        with patch.object(self.service, "send_email_async") as async_mock, patch(
            "somewheria_app.services.notifications.time.monotonic",
            side_effect=[0.0, 1.0, 10_000.0, 10_000.0],
        ):
            self.service.log_and_notify_error("Save Error", "first")
            self.service.log_and_notify_error("Save Error", "suppressed")
            self.service.log_and_notify_error("Save Error", "after cooldown")

        self.assertEqual(async_mock.call_count, 2)
        self.assertEqual(async_mock.call_args_list[0].args, ("Save Error", "first"))
        self.assertEqual(async_mock.call_args_list[1].args, ("Save Error", "after cooldown"))

    def test_log_and_notify_error_prunes_stale_dedup_entries(self):
        # A long-running process that trips many distinct subjects must not
        # grow the dedup map without bound — anything older than the cooldown
        # can no longer suppress a send and has no value. Prime the map with
        # a stale entry, then send a fresh event and assert the stale key
        # was evicted.
        stale_key = "Zillow Sync Failure"
        # A monotonic value in the deep past — guaranteed to be older than
        # any cooldown window we'd pick.
        self.service._error_email_last_sent[stale_key] = -10_000.0

        with patch.object(self.service, "send_email_async"):
            self.service.log_and_notify_error("Save Error", "fresh")

        self.assertNotIn(stale_key, self.service._error_email_last_sent)
        self.assertIn("Save Error", self.service._error_email_last_sent)

    def test_notify_image_edit_dispatches_async(self):
        # notify_image_edit MUST NOT block the caller on SMTP delivery — its
        # call sites (properties.upload_image + admin image_edit_notify) run
        # on the request thread and would otherwise inherit up to
        # SMTP_TIMEOUT_SECONDS (30 s) of Gmail-relay latency per upload.
        # Assert dispatch happens through send_email_async so the pattern
        # matches the other request-hot notification sites (PRs #136-#138).
        with patch.object(self.service, "send_email_async") as async_mock:
            self.service.notify_image_edit(["https://example.com/a.jpg", "https://example.com/b.jpg"])

        async_mock.assert_called_once()
        self.assertEqual(async_mock.call_args[0][0], "Image Edited Notification")
        self.assertIn("https://example.com/a.jpg", async_mock.call_args[0][1])

    def test_notify_image_edit_delivery_matches_disable_background_threads(self):
        # Under DISABLE_BACKGROUND_THREADS=1 the async wrapper runs send_email
        # inline in the caller's thread, so mocking send_email is still enough
        # to observe (or suppress) image-edit notifications — the invariant
        # that other tests (e.g. properties.upload_image) rely on.
        with patch.dict(os.environ, {"DISABLE_BACKGROUND_THREADS": "1"}), patch.object(
            self.service, "send_email"
        ) as send_email_mock:
            self.service.notify_image_edit(["https://example.com/a.jpg"])

        send_email_mock.assert_called_once()
        self.assertEqual(send_email_mock.call_args[0][0], "Image Edited Notification")
        self.assertIn("https://example.com/a.jpg", send_email_mock.call_args[0][1])

    def test_send_email_async_runs_inline_when_threads_disabled(self):
        # DISABLE_BACKGROUND_THREADS=1 forces the wrapper to run send_email in
        # the caller's thread so tests can assert against the delivery attempt
        # directly (matches ticket-email + zillow-publish dispatch pattern).
        with patch.dict(os.environ, {"DISABLE_BACKGROUND_THREADS": "1"}), patch.object(
            self.service, "send_email"
        ) as send_email_mock:
            self.service.send_email_async("Subject", "Body", to="a@b.com")

        send_email_mock.assert_called_once_with("Subject", "Body", to="a@b.com")

    def test_send_email_async_swallows_inline_failures(self):
        # A raise from send_email must NOT propagate to the caller — the
        # wrapper's whole point is fire-and-forget dispatch. Same contract as
        # TicketService._enqueue_email.
        with patch.dict(os.environ, {"DISABLE_BACKGROUND_THREADS": "1"}), patch.object(
            self.service, "send_email", side_effect=RuntimeError("boom")
        ):
            # Must not raise.
            self.service.send_email_async("Subject", "Body")

    def test_send_email_async_dispatches_to_daemon_thread(self):
        # With backgrounding enabled the wrapper must return without waiting
        # on send_email, so a slow SMTP relay can't stall the request thread.
        with patch.dict(os.environ, {}, clear=False):
            # Ensure the env hatch is OFF for this test regardless of parent.
            os.environ.pop("DISABLE_BACKGROUND_THREADS", None)
            try:
                with patch(
                    "somewheria_app.services.notifications.threading.Thread"
                ) as thread_ctor:
                    thread_instance = Mock()
                    thread_ctor.return_value = thread_instance
                    self.service.send_email_async("S", "B")
            finally:
                os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

        thread_ctor.assert_called_once()
        kwargs = thread_ctor.call_args.kwargs
        self.assertTrue(kwargs.get("daemon"))
        thread_instance.start.assert_called_once()

    def test_log_site_change_writes_json_line(self):
        handle = mock_open()
        with patch.object(Path, "open", handle):
            self.service.log_site_change("admin@example.com", "property_updated", {"property_id": "prop-1"})

        written = "".join(call.args[0] for call in handle().write.call_args_list)
        self.assertIn('"user": "admin@example.com"', written)
        self.assertIn('"action": "property_updated"', written)
        self.assertIn('"property_id": "prop-1"', written)

    def test_read_logs_returns_empty_list_when_log_file_missing(self):
        with patch.object(Path, "exists", return_value=False):
            entries = self.service.read_logs()

        self.assertEqual(entries, [])

    def test_read_logs_parses_pipe_and_legacy_formats(self):
        log_text = (
            "2026-03-23 18:47:42|INFO|http|GET /admin/status -> 200 in 23.55ms\n"
            "2026-03-23:WARN:Legacy warning line\n"
        )
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "open",
            mock_open(read_data=log_text),
        ):
            entries = self.service.read_logs()

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["level"], "WARNING")
        self.assertIn("Legacy warning line", entries[0]["message"])
        self.assertEqual(entries[1]["level"], "INFO")
        self.assertIn("[http] GET /admin/status -> 200", entries[1]["message"])

    def test_read_logs_parses_json_lines_with_request_id(self):
        log_text = (
            '{"timestamp": "2026-07-06T18:47:42", "level": "ERROR", '
            '"component": "http", "request_id": "a1b2c3d4", '
            '"message": "GET /for-rent -> 500"}\n'
            '{"timestamp": "2026-07-06T18:47:43", "level": "INFO", '
            '"component": "app", "request_id": "-", "message": "started"}\n'
        )
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "open", mock_open(read_data=log_text)
        ):
            entries = self.service.read_logs()

        self.assertEqual(len(entries), 2)
        # Newest-first ordering.
        self.assertEqual(entries[1]["level"], "ERROR")
        self.assertEqual(entries[1]["request_id"], "a1b2c3d4")
        self.assertIn("[http] GET /for-rent -> 500", entries[1]["message"])
        self.assertEqual(entries[0]["request_id"], "-")

    def test_read_logs_caps_returned_entries_to_500(self):
        # A log file with more than 500 entries should still return only the
        # last 500, in newest-first order. The implementation streams the
        # file through a bounded deque so memory does not grow with the
        # number of historical lines.
        log_text = "".join(
            f"2026-03-23 18:47:{i % 60:02d}|INFO|http|line {i}\n"
            for i in range(700)
        )
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path,
            "open",
            mock_open(read_data=log_text),
        ):
            entries = self.service.read_logs()

        self.assertEqual(len(entries), 500)
        # Newest entry first.
        self.assertIn("line 699", entries[0]["message"])
        # Oldest retained entry is index 200 (700 - 500).
        self.assertIn("line 200", entries[-1]["message"])

    def test_read_logs_skips_non_string_fields_in_json_rows(self):
        # A corrupted / hand-edited JSON log row where ``message`` is ``null``
        # (or a bare number) used to crash the viewer at
        # ``ansi_escape.sub("", message)`` with ``TypeError`` when
        # ``component`` was absent (the intervening f-string couldn't coerce
        # it first), taking the /logs page out via the crash handler's 503.
        # Every field should now coerce safely to a string so the row still
        # renders.
        log_text = (
            '{"timestamp": "2026-07-06T18:47:42", "level": "INFO", '
            '"request_id": "abc12345", "message": null}\n'
            '{"timestamp": "2026-07-06T18:47:43", "level": "INFO", '
            '"request_id": "def67890", "message": 42}\n'
            '{"timestamp": 123, "level": 7, "request_id": null, '
            '"message": "ok"}\n'
        )
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "open", mock_open(read_data=log_text)
        ):
            entries = self.service.read_logs()

        self.assertEqual(len(entries), 3)
        # Newest-first ordering.
        self.assertEqual(entries[2]["message"], "")
        self.assertEqual(entries[2]["request_id"], "abc12345")
        self.assertEqual(entries[1]["message"], "42")
        self.assertEqual(entries[1]["request_id"], "def67890")
        # Non-string request_id falls back to "-"; non-string level/timestamp
        # coerce to their str() form so the row still renders.
        self.assertEqual(entries[0]["request_id"], "-")
        self.assertEqual(entries[0]["level"], "7")
        self.assertEqual(entries[0]["timestamp"], "123")
        self.assertEqual(entries[0]["message"], "ok")


class PropertyWritePathTestCase(unittest.TestCase):
    def setUp(self):
        self.notifications = Mock()
        self.config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        self.service = PropertyService(self.config, self.notifications)

    def test_property_payload_from_form_includes_sqft(self):
        form = DummyForm(values={"name": "Maple", "sqft": "1200"})

        payload = self.service.property_payload_from_form(form)

        self.assertEqual(payload["sqft"], "1200")

    def test_update_property_forwards_sqft_to_upstream(self):
        current = {
            "id": "prop-1",
            "name": "Maple",
            "address": "123 Main",
            "rent": "1000",
            "deposit": "1000",
            "sqft": "900",
            "bedrooms": "2",
            "bathrooms": "1",
            "lease_length": "12 months",
            "pets_allowed": "No",
            "blurb": "Old",
            "description": "Old description",
        }
        form = DummyForm(values={"sqft": "1500"})

        with patch.object(self.service, "get_property", return_value=current), patch(
            "somewheria_app.services.properties.requests.put"
        ) as put_mock, patch.object(self.service, "trigger_background_refresh"):
            self.service.update_property("prop-1", form, "admin@example.com")

        self.assertEqual(put_mock.call_args.kwargs["json"]["sqft"], "1500")

    def test_update_property_raises_when_upstream_rejects(self):
        current = {"id": "prop-1", "name": "Maple"}
        form = DummyForm(values={"name": "Updated"})
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("upstream 500")

        with patch.object(self.service, "get_property", return_value=current), patch(
            "somewheria_app.services.properties.requests.put",
            return_value=response,
        ), patch.object(self.service, "trigger_background_refresh") as trigger_mock:
            with self.assertRaises(RuntimeError):
                self.service.update_property("prop-1", form, "admin@example.com")

        # The upstream rejected, so we must NOT log the change or kick a refresh.
        self.notifications.log_site_change.assert_not_called()
        trigger_mock.assert_not_called()

    def test_toggle_sale_does_not_update_cache_when_upstream_fails(self):
        self.service.cache = [{"id": "prop-1", "for_sale": False, "status": "Active"}]
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("upstream 500")

        with patch(
            "somewheria_app.services.properties.requests.put",
            return_value=response,
        ):
            with self.assertRaises(RuntimeError):
                self.service.toggle_sale("prop-1", "admin@example.com")

        self.assertFalse(self.service.cache[0]["for_sale"])
        self.assertEqual(self.service.cache[0]["status"], "Active")
        self.notifications.log_site_change.assert_not_called()

    def test_safe_json_returns_default_on_http_error_status(self):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("404")
        with patch(
            "somewheria_app.services.properties.requests.get",
            return_value=response,
        ):
            payload = self.service._safe_json("https://example.com/data", ["fallback"])

        self.assertEqual(payload, ["fallback"])

    def test_safe_json_returns_default_when_type_does_not_match(self):
        # Caller asked for a list but the API answered with an object. The
        # default must be returned so downstream iteration doesn't quietly
        # walk the dict's keys.
        response = Mock()
        response.json.return_value = {"oops": "object"}
        with patch(
            "somewheria_app.services.properties.requests.get",
            return_value=response,
        ):
            payload = self.service._safe_json(
                "https://example.com/photos", ["fallback"], expected_type=list
            )

        self.assertEqual(payload, ["fallback"])

    def test_safe_json_returns_payload_when_type_matches(self):
        response = Mock()
        response.json.return_value = ["a", "b"]
        with patch(
            "somewheria_app.services.properties.requests.get",
            return_value=response,
        ):
            payload = self.service._safe_json(
                "https://example.com/photos", [], expected_type=list
            )

        self.assertEqual(payload, ["a", "b"])

    def test_fetch_property_record_skips_invalid_property_id(self):
        # Defense in depth: even if upstream somehow returned a malformed
        # id, we must not relay it into an outbound URL.
        with patch("somewheria_app.services.properties.requests.get") as get_mock:
            result = self.service.fetch_property_record("../etc")

        self.assertIsNone(result)
        get_mock.assert_not_called()

    def test_fetch_property_record_ignores_non_string_photo_urls(self):
        details_response = Mock()
        details_response.json.return_value = {"name": "House"}
        photos_response = Mock()
        # A misbehaving upstream returns a list with non-string entries.
        # Those must be skipped instead of crashing the image pipeline.
        photos_response.json.return_value = [42, None, "https://example.com/p.jpg"]
        thumb_response = Mock()
        thumb_response.json.return_value = "https://example.com/thumb.jpg"

        responses = {
            f"{self.service.config.api_base_url}/properties/prop-1/details": details_response,
            f"{self.service.config.api_base_url}/properties/prop-1/photos": photos_response,
            f"{self.service.config.api_base_url}/properties/prop-1/thumbnail": thumb_response,
        }

        def fake_get(url, *args, **kwargs):
            return responses[url]

        with patch("somewheria_app.services.properties.requests.get", side_effect=fake_get), patch.object(
            self.service, "get_base64_image_from_url"
        ) as encode_mock:
            record = self.service.fetch_property_record("prop-1")

        # Photos are kept as S3 URLs (loaded by the browser directly), not
        # downloaded/base64-encoded here — the encoder is never called, and
        # non-string entries are dropped.
        encode_mock.assert_not_called()
        self.assertIsNotNone(record)
        self.assertEqual(record["photos"], ["https://example.com/p.jpg"])


class AnalyticsPruningTestCase(unittest.TestCase):
    def test_prune_drops_buckets_outside_window(self):
        from somewheria_app.services.analytics import AnalyticsTracker

        tracker = AnalyticsTracker(analytics_days=3)
        tracker.site_visits["2024-01-01"] = 5  # well outside the 3-day window
        tracker.unique_users["2024-01-01"] = {"old@example.com"}
        tracker.logins["2024-01-01"] = 1
        tracker.errors["2024-01-01"] = 2

        # Today happens to be 2026-05-01 in this test environment but the
        # prune logic uses whatever string we pass in, so this is independent
        # of wall-clock time.
        tracker._prune_old_buckets("2030-01-10")

        self.assertNotIn("2024-01-01", tracker.site_visits)
        self.assertNotIn("2024-01-01", tracker.unique_users)
        self.assertNotIn("2024-01-01", tracker.logins)
        self.assertNotIn("2024-01-01", tracker.errors)

    def test_prune_keeps_recent_days(self):
        from somewheria_app.services.analytics import AnalyticsTracker

        tracker = AnalyticsTracker(analytics_days=7)
        tracker.site_visits["2030-01-08"] = 4  # 2 days before "today"
        tracker.site_visits["2030-01-10"] = 1  # the test "today"

        tracker._prune_old_buckets("2030-01-10")

        self.assertEqual(tracker.site_visits["2030-01-08"], 4)
        self.assertEqual(tracker.site_visits["2030-01-10"], 1)

    def test_concurrent_prune_does_not_race(self):
        """Many threads pruning simultaneously must not raise.

        Without the lock guarding ``_prune_old_buckets``, two threads can race
        such that one's list-comprehension snapshot of ``bucket.keys()`` is
        invalidated by another's ``del``, raising RuntimeError ("dictionary
        changed size during iteration") or KeyError. The fix wraps the
        read-modify-write under ``_lock``.
        """
        import sys
        import threading
        from somewheria_app.services.analytics import AnalyticsTracker

        tracker = AnalyticsTracker(analytics_days=3)
        for i in range(500):
            day = f"2024-01-{(i % 28) + 1:02d}"
            tracker.site_visits[day] = i
            tracker.unique_users[day].add(f"user-{i}@example.com")
            tracker.logins[day] = i
            tracker.errors[day] = i

        errors: list[BaseException] = []
        barrier = threading.Barrier(16)
        # Tighten the GC switch interval so threads actually contend.
        original_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.00001)

        def worker():
            try:
                barrier.wait()
                with tracker._lock:
                    tracker._prune_old_buckets("2030-01-10")
            except BaseException as exc:  # noqa: BLE001 - test must surface any
                errors.append(exc)

        try:
            threads = [threading.Thread(target=worker) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(original_interval)

        self.assertEqual(errors, [])
        # All historical buckets should be gone; the dict-level dispatch must
        # have completed cleanly across every thread.
        self.assertEqual(len(tracker.site_visits), 0)
        self.assertEqual(len(tracker.unique_users), 0)
        self.assertEqual(len(tracker.logins), 0)
        self.assertEqual(len(tracker.errors), 0)


class DashboardDataDayBoundaryTestCase(unittest.TestCase):
    """`dashboard_data` reads the current date once. The old code called
    ``date.today()`` for ``today`` and then again inside the ``days`` list
    comprehension; a request that happened to straddle midnight between those
    two reads had ``metrics["site_visits"]`` bucketed under yesterday while
    the chart's rightmost day labeled today, so the same request rendered
    inconsistent numbers for the metric card and the chart."""

    def test_today_matches_last_day_across_midnight(self):
        import somewheria_app.services.analytics as analytics_module
        from somewheria_app.services.analytics import AnalyticsTracker

        real_datetime_module = analytics_module.datetime
        # Advance the "clock" by one day on the second read. Under the old
        # implementation the metrics bucket read the day-1 value while the
        # chart's rightmost label was day-2 — visibly inconsistent.
        returned = [
            real_datetime_module.date(2026, 7, 4),
            real_datetime_module.date(2026, 7, 5),
        ]

        class _AdvancingDate(real_datetime_module.date):
            @classmethod
            def today(cls):
                return returned.pop(0) if returned else real_datetime_module.date(2026, 7, 5)

        tracker = AnalyticsTracker(analytics_days=3)
        # Seed both possible "today" buckets so we can prove which one won.
        tracker.site_visits["2026-07-04"] = 42
        tracker.site_visits["2026-07-05"] = 7

        fake_module = SimpleNamespace(
            date=_AdvancingDate,
            datetime=real_datetime_module.datetime,
            timedelta=real_datetime_module.timedelta,
            timezone=real_datetime_module.timezone,
        )
        with patch.object(analytics_module, "datetime", fake_module):
            metrics, chart_data = tracker.dashboard_data(property_count=0)

        # ``today`` was read exactly once — so ``days[-1]`` and the bucket the
        # ``site_visits`` metric read from must agree. Without the fix the
        # metric would be 7 (from the second ``date.today()`` call) while the
        # chart's last label would still read "2026-07-04" — or vice versa.
        self.assertEqual(chart_data["days"][-1], "2026-07-04")
        self.assertEqual(metrics["site_visits"], 42)


class RecentListingActivityTestCase(unittest.TestCase):
    """`recent_listing_activity` buckets entries by the UTC ``YYYY-MM`` prefix
    of the change-log timestamp (`utcnow_iso()` writes Z-suffixed UTC). Labels
    must also come from UTC so a fresh entry logged just after the UTC month
    rolls over still lands in the current-window buckets when the server's
    local calendar hasn't rolled yet — otherwise the current month's data is
    silently dropped from the admin chart."""

    def _tracker(self, tmpdir: Path):
        from somewheria_app.services.analytics import AnalyticsTracker

        change_log = tmpdir / "site_changes.log"
        config = SimpleNamespace(change_log_file=change_log)
        return AnalyticsTracker(analytics_days=7, config=config), change_log

    def test_labels_use_utc_not_local_time(self):
        # Simulate a server whose local calendar reads 2026-06-30 while
        # UTC has already rolled over to 2026-07-01. Under the old code,
        # ``date.today()`` returned the local date, labels ended at
        # "2026-06", and any entry timestamped with the current UTC month
        # ("2026-07-...Z") fell outside the label set and was silently
        # dropped from the admin chart.
        import somewheria_app.services.analytics as analytics_module

        real_datetime_module = analytics_module.datetime

        class _FrozenDate(real_datetime_module.date):
            @classmethod
            def today(cls):
                # Local calendar still reads the previous UTC month.
                return real_datetime_module.date(2026, 6, 30)

        class _FrozenDatetime(real_datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                # UTC has already rolled over to the next month.
                return real_datetime_module.datetime(
                    2026, 7, 1, 6, 30, 0,
                    tzinfo=tz or real_datetime_module.timezone.utc,
                )

        with tempfile.TemporaryDirectory() as td:
            tracker, change_log = self._tracker(Path(td))
            change_log.write_text(
                '{"timestamp": "2026-07-01T06:31:00Z", "action": "property_created", "extra": {}}\n'
                '{"timestamp": "2026-06-30T23:59:00Z", "action": "property_created", "extra": {}}\n',
                encoding="utf-8",
            )

            fake_module = SimpleNamespace(
                date=_FrozenDate,
                datetime=_FrozenDatetime,
                timedelta=real_datetime_module.timedelta,
                timezone=real_datetime_module.timezone,
            )
            with patch.object(analytics_module, "datetime", fake_module):
                result = tracker.recent_listing_activity(months=3)

        # Current-window labels must include the current UTC month.
        self.assertIn("2026-07", result["months"])
        self.assertIn("2026-06", result["months"])
        # And the UTC-July entry must be counted, not silently dropped.
        july_idx = result["months"].index("2026-07")
        june_idx = result["months"].index("2026-06")
        self.assertEqual(result["created"][july_idx], 1)
        self.assertEqual(result["created"][june_idx], 1)

    def test_survives_non_dict_json_lines_in_change_log(self):
        # A row that parses as valid JSON but is not a dict (a bare number,
        # string, list, or ``null``) used to crash the whole function with
        # AttributeError on ``entry.get("action")`` — which the admin
        # dashboard call site then served as a 503. The writer only ever
        # emits dict rows, so this can only happen via a corrupted / hand-
        # edited / externally-appended log, but a single bad line must not
        # take out /admin/dashboard for everyone.
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            tracker, change_log = self._tracker(Path(td))
            today_iso = (
                datetime.datetime.now(datetime.timezone.utc)
                .date()
                .isoformat()
            )
            rows = [
                "42",
                '"just a string"',
                "null",
                "[1, 2, 3]",
                # A dict with a non-string timestamp shouldn't crash either.
                _json.dumps({"timestamp": 12345, "action": "property_created"}),
                _json.dumps(
                    {
                        "timestamp": f"{today_iso}T00:00:00Z",
                        "action": "property_created",
                        "extra": {},
                    }
                ),
            ]
            change_log.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = tracker.recent_listing_activity(months=1)
        # The one valid ``property_created`` row still lands in this month's
        # bucket; the malformed rows are silently skipped rather than crashing.
        self.assertEqual(sum(result["created"]), 1)
        self.assertEqual(sum(result["deleted"]), 0)


class RateLimiterTestCase(unittest.TestCase):
    def _make_limiter(self):
        from somewheria_app.services.security import _RateLimiter

        return _RateLimiter()

    def test_check_allows_until_limit_then_blocks_within_window(self):
        limiter = self._make_limiter()

        self.assertTrue(limiter.check("k", limit=2, window_seconds=60))
        self.assertTrue(limiter.check("k", limit=2, window_seconds=60))
        self.assertFalse(limiter.check("k", limit=2, window_seconds=60))

    def test_unseen_key_lookup_does_not_create_entry(self):
        limiter = self._make_limiter()

        # Reading the internal map must not auto-create empty buckets — the
        # earlier ``defaultdict(deque)`` implementation leaked one entry per
        # unique (endpoint, IP) pair seen.
        self.assertEqual(len(limiter._hits), 0)
        _ = limiter._hits.get("never-seen")
        self.assertEqual(len(limiter._hits), 0)

    def test_blocked_request_does_not_extend_bucket(self):
        limiter = self._make_limiter()

        self.assertTrue(limiter.check("k", limit=1, window_seconds=60))
        self.assertFalse(limiter.check("k", limit=1, window_seconds=60))

        # A blocked check must not append a new timestamp; otherwise a flood
        # of denied requests would keep extending the window indefinitely.
        self.assertEqual(len(limiter._hits["k"]), 1)

    def test_sweep_drops_stale_keys_only(self):
        limiter = self._make_limiter()
        limiter.check("active", limit=5, window_seconds=60)
        limiter.check("stale", limit=5, window_seconds=60)

        # Backdate the "stale" key's only timestamp past the TTL.
        limiter._hits["stale"][-1] = (
            limiter._hits["active"][-1] - limiter._STALE_KEY_TTL_SECONDS - 10
        )

        with limiter._lock:
            limiter._sweep_stale_keys(limiter._hits["active"][-1])

        self.assertIn("active", limiter._hits)
        self.assertNotIn("stale", limiter._hits)

    def test_sweep_runs_periodically_during_check(self):
        limiter = self._make_limiter()
        old = time.monotonic() - limiter._STALE_KEY_TTL_SECONDS - 100
        limiter._hits["stale"] = deque([old])

        # Drive enough check() calls on a *different* key to trigger one
        # full sweep cycle without refreshing the stale bucket.
        for _ in range(limiter._SWEEP_INTERVAL_CALLS):
            limiter.check("active", limit=10**9, window_seconds=60)

        self.assertNotIn("stale", limiter._hits)
        self.assertIn("active", limiter._hits)


class ClientIpResolutionTestCase(unittest.TestCase):
    """`_client_ip` keys the rate limiter. If it honors a client-supplied
    ``X-Forwarded-For`` without a trusted-proxy guard, an attacker can rotate
    the header per request to gain a fresh bucket and bypass throttles."""

    def _build_app(self, trusted_proxy_count=0):
        from flask import Flask
        from werkzeug.middleware.proxy_fix import ProxyFix

        app = Flask(__name__)
        app.secret_key = "test"
        if trusted_proxy_count > 0:
            app.wsgi_app = ProxyFix(
                app.wsgi_app,
                x_for=trusted_proxy_count,
                x_proto=trusted_proxy_count,
                x_host=trusted_proxy_count,
            )
        return app

    def _resolve(self, app, environ_overrides=None, headers=None):
        from somewheria_app.services.security import _client_ip

        overrides = {"REMOTE_ADDR": "10.0.0.1"}
        if environ_overrides:
            overrides.update(environ_overrides)
        with app.test_request_context(
            "/", environ_overrides=overrides, headers=headers or {}
        ):
            return _client_ip()

    def test_default_ignores_x_forwarded_for(self):
        app = self._build_app(trusted_proxy_count=0)
        ip = self._resolve(
            app,
            headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
        )
        # Header is attacker-controlled when no proxy is declared; we
        # MUST fall back to the actual TCP peer.
        self.assertEqual(ip, "10.0.0.1")

    def test_default_uses_remote_addr_when_no_header(self):
        app = self._build_app(trusted_proxy_count=0)
        ip = self._resolve(app)
        self.assertEqual(ip, "10.0.0.1")

    def test_default_handles_missing_remote_addr(self):
        app = self._build_app(trusted_proxy_count=0)
        ip = self._resolve(app, environ_overrides={"REMOTE_ADDR": None})
        # Werkzeug stores None as missing; _client_ip falls back to the
        # explicit sentinel so the bucket key is never empty.
        self.assertEqual(ip, "0.0.0.0")

    def test_default_does_not_let_spoofed_header_split_buckets(self):
        app = self._build_app(trusted_proxy_count=0)
        first = self._resolve(app, headers={"X-Forwarded-For": "1.1.1.1"})
        second = self._resolve(app, headers={"X-Forwarded-For": "2.2.2.2"})
        # An attacker rotating XFF from a single TCP peer must hit the
        # same limiter bucket — otherwise the rate limit is bypassable.
        self.assertEqual(first, second)
        self.assertEqual(first, "10.0.0.1")

    def _resolve_via_wsgi(self, app, headers=None, remote_addr="10.0.0.99"):
        # ProxyFix runs at WSGI middleware layer, so test_request_context
        # bypasses it. Drive the test client instead — it executes the
        # full middleware chain before the view runs.
        from somewheria_app.services.security import _client_ip

        captured = {}

        @app.route("/__client_ip__")
        def _capture():
            captured["ip"] = _client_ip()
            return "ok"

        client = app.test_client()
        client.get(
            "/__client_ip__",
            headers=headers or {},
            environ_overrides={"REMOTE_ADDR": remote_addr},
        )
        return captured["ip"]

    def test_with_trusted_proxy_extracts_real_client_from_xff(self):
        # Operator declared ONE trusted proxy in front of the app.
        # ProxyFix strips the rightmost hop and exposes the original
        # client IP as remote_addr; _client_ip should surface that.
        app = self._build_app(trusted_proxy_count=1)
        ip = self._resolve_via_wsgi(
            app,
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        self.assertEqual(ip, "203.0.113.5")

    def test_with_trusted_proxy_falls_back_when_header_missing(self):
        app = self._build_app(trusted_proxy_count=1)
        ip = self._resolve_via_wsgi(app)
        # No header means we still see the proxy IP — not great, but the
        # expected behavior; the operator's job is to ensure the proxy
        # always sets X-Forwarded-For.
        self.assertEqual(ip, "10.0.0.99")


class TrustedProxyConfigTestCase(unittest.TestCase):
    """``TRUSTED_PROXY_COUNT`` parsing must fail closed: any non-numeric or
    negative value reverts to 0 (ignore X-Forwarded-For) rather than
    enabling proxy trust with an undefined hop count."""

    def _load_count(self, raw):
        import os
        from importlib import reload

        import somewheria_app.config as cfg

        previous = os.environ.get("TRUSTED_PROXY_COUNT")
        if raw is None:
            os.environ.pop("TRUSTED_PROXY_COUNT", None)
        else:
            os.environ["TRUSTED_PROXY_COUNT"] = raw
        try:
            reload(cfg)
            return cfg.AppConfig().trusted_proxy_count
        finally:
            if previous is None:
                os.environ.pop("TRUSTED_PROXY_COUNT", None)
            else:
                os.environ["TRUSTED_PROXY_COUNT"] = previous
            reload(cfg)

    def test_unset_defaults_to_zero(self):
        self.assertEqual(self._load_count(None), 0)

    def test_blank_defaults_to_zero(self):
        self.assertEqual(self._load_count("   "), 0)

    def test_non_numeric_defaults_to_zero(self):
        self.assertEqual(self._load_count("nginx"), 0)

    def test_negative_defaults_to_zero(self):
        self.assertEqual(self._load_count("-1"), 0)

    def test_valid_integer_parses(self):
        self.assertEqual(self._load_count("2"), 2)


class CacheRefreshIntervalConfigTestCase(unittest.TestCase):
    """``CACHE_REFRESH_INTERVAL`` must tolerate malformed env values the same
    way ``TRUSTED_PROXY_COUNT`` does: the app has to boot even when an
    operator typos the number, rather than raising ValueError inside the
    dataclass ``default_factory`` and crashing at startup."""

    def _load_interval(self, raw):
        import os
        from importlib import reload

        import somewheria_app.config as cfg

        previous = os.environ.get("CACHE_REFRESH_INTERVAL")
        if raw is None:
            os.environ.pop("CACHE_REFRESH_INTERVAL", None)
        else:
            os.environ["CACHE_REFRESH_INTERVAL"] = raw
        try:
            reload(cfg)
            return cfg.AppConfig().cache_refresh_interval
        finally:
            if previous is None:
                os.environ.pop("CACHE_REFRESH_INTERVAL", None)
            else:
                os.environ["CACHE_REFRESH_INTERVAL"] = previous
            reload(cfg)

    def test_unset_defaults_to_sixty(self):
        self.assertEqual(self._load_interval(None), 60)

    def test_blank_defaults_to_sixty(self):
        self.assertEqual(self._load_interval("   "), 60)

    def test_non_numeric_defaults_to_sixty(self):
        # Before the fix, ``int("sixty")`` raised ValueError inside the
        # dataclass default_factory and prevented ``create_app()`` from
        # succeeding — the whole process failed to boot on a mistyped env.
        self.assertEqual(self._load_interval("sixty"), 60)

    def test_negative_defaults_to_sixty(self):
        # ``_int_env`` treats negatives as invalid so the polling loop
        # (and admin status card) never see a nonsense window.
        self.assertEqual(self._load_interval("-5"), 60)

    def test_valid_integer_parses(self):
        self.assertEqual(self._load_interval("120"), 120)


class CsrfTokenExtractionTestCase(unittest.TestCase):
    """``_extract_submitted_token`` must always return a string so the
    ``secrets.compare_digest`` check in ``_csrf_before_request`` can never
    raise TypeError on a malformed body — that would surface as a 500 instead
    of the intended 400."""

    def _build_app(self):
        from flask import Flask

        app = Flask(__name__)
        app.secret_key = "test"
        return app

    def _extract(self, app, **request_kwargs):
        from somewheria_app.services.security import _extract_submitted_token

        with app.test_request_context(**request_kwargs):
            return _extract_submitted_token()

    def test_returns_string_for_header(self):
        app = self._build_app()
        token = self._extract(app, method="POST", headers={"X-CSRF-Token": "abc"})
        self.assertEqual(token, "abc")

    def test_returns_empty_when_nothing_submitted(self):
        app = self._build_app()
        self.assertEqual(self._extract(app, method="POST"), "")

    def test_coerces_non_string_json_token_to_empty(self):
        app = self._build_app()
        # JSON body where _csrf_token is a list — passing this to
        # secrets.compare_digest would raise TypeError. The extractor must
        # treat it as missing instead.
        for bogus in ([1, 2, 3], {"nested": "x"}, 42, None):
            token = self._extract(
                app,
                method="POST",
                json={"_csrf_token": bogus},
            )
            self.assertEqual(token, "", f"non-string token {bogus!r} should coerce to ''")
            self.assertIsInstance(token, str)

    def test_accepts_string_json_token(self):
        app = self._build_app()
        token = self._extract(
            app,
            method="POST",
            json={"_csrf_token": "json-token"},
        )
        self.assertEqual(token, "json-token")


class TourUrlSanitizationTestCase(unittest.TestCase):
    def test_blank_property_includes_tour_url(self):
        from somewheria_app.services.properties import BLANK_PROPERTY

        self.assertIn("tour_url", BLANK_PROPERTY)
        self.assertEqual(BLANK_PROPERTY["tour_url"], "")

    def test_sanitize_accepts_https_url(self):
        from somewheria_app.services.properties import sanitize_tour_url

        self.assertEqual(
            sanitize_tour_url("https://my.matterport.com/show/?m=abc"),
            "https://my.matterport.com/show/?m=abc",
        )

    def test_sanitize_rejects_javascript_scheme(self):
        from somewheria_app.services.properties import sanitize_tour_url

        for hostile in (
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)  ",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "",
            "   ",
            "not a url",
            "/relative/path",
            None,
            123,
        ):
            self.assertEqual(sanitize_tour_url(hostile), "")

    def test_sanitize_rejects_userinfo_in_authority(self):
        from somewheria_app.services.properties import sanitize_tour_url

        for hostile in (
            "https://user:pass@evil.com/show",
            "https://user@evil.com/show",
            "http://admin:hunter2@matterport.com/show",
        ):
            self.assertEqual(sanitize_tour_url(hostile), "")

    def test_sanitize_rejects_control_characters(self):
        from somewheria_app.services.properties import sanitize_tour_url

        for hostile in (
            "https://example.com\n/show",
            "https://example.com\r\n/show",
            "https://example.com\t/show",
            "https://example.com\x00.evil.com/show",
            "https://example.com\x1f/show",
            "https://example.com\x7f/show",
        ):
            self.assertEqual(sanitize_tour_url(hostile), "")

    def test_sanitize_rejects_backslashes(self):
        from somewheria_app.services.properties import sanitize_tour_url

        for hostile in (
            "https://evil.com\\@target.com/show",
            "https://example.com/\\evil/show",
            "https:\\\\example.com/show",
        ):
            self.assertEqual(sanitize_tour_url(hostile), "")

    def test_property_payload_from_form_includes_sanitized_tour_url(self):
        notifications = Mock()
        config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        service = PropertyService(config, notifications)
        form = DummyForm(values={"tour_url": "https://kuula.co/share/abc"})

        payload = service.property_payload_from_form(form)

        self.assertEqual(payload["tour_url"], "https://kuula.co/share/abc")

    def test_property_payload_drops_javascript_tour_url(self):
        notifications = Mock()
        config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        service = PropertyService(config, notifications)
        form = DummyForm(values={"tour_url": "javascript:alert(1)"})

        payload = service.property_payload_from_form(form)

        self.assertEqual(payload["tour_url"], "")

    def test_update_property_forwards_sanitized_tour_url_to_upstream(self):
        notifications = Mock()
        config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        service = PropertyService(config, notifications)
        current = {"id": "prop-1", "name": "Maple", "tour_url": ""}
        form = DummyForm(values={"tour_url": "https://my.matterport.com/show/?m=xyz"})

        with patch.object(service, "get_property", return_value=current), patch(
            "somewheria_app.services.properties.requests.put"
        ) as put_mock, patch.object(service, "trigger_background_refresh"):
            service.update_property("prop-1", form, "admin@example.com")

        self.assertEqual(
            put_mock.call_args.kwargs["json"]["tour_url"],
            "https://my.matterport.com/show/?m=xyz",
        )

    def test_update_property_drops_hostile_tour_url(self):
        notifications = Mock()
        config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        service = PropertyService(config, notifications)
        current = {"id": "prop-1", "name": "Maple", "tour_url": ""}
        form = DummyForm(values={"tour_url": "javascript:alert(1)"})

        with patch.object(service, "get_property", return_value=current), patch(
            "somewheria_app.services.properties.requests.put"
        ) as put_mock, patch.object(service, "trigger_background_refresh"):
            service.update_property("prop-1", form, "admin@example.com")

        self.assertEqual(put_mock.call_args.kwargs["json"]["tour_url"], "")


class ZillowPublisherTestCase(unittest.TestCase):
    def setUp(self):
        from somewheria_app.services.zillow import ZillowPublisher

        self.notifications = Mock()
        self.config = SimpleNamespace()
        # Wipe any inherited env so the unconfigured cases are deterministic.
        self._env_patch = patch.dict(
            "os.environ",
            {"ZILLOW_API_BASE_URL": "", "ZILLOW_API_TOKEN": "", "ZILLOW_FEED_KEY": ""},
            clear=False,
        )
        self._env_patch.start()
        self.publisher = ZillowPublisher(self.config, self.notifications)

    def tearDown(self):
        self._env_patch.stop()

    def test_publish_create_without_credentials_logs_and_returns(self):
        # No env vars set: every method must return without raising and
        # without spawning a worker.
        self.publisher.publish_create({"id": "prop-1"})
        self.publisher.publish_update({"id": "prop-1"})
        self.publisher.publish_delete("prop-1")
        self.publisher.publish_for_sale_toggle("prop-1", True)
        self.assertFalse(self.publisher.credentials_configured())
        self.assertEqual(self.publisher.success_count, 0)
        self.assertEqual(self.publisher.failure_count, 0)
        self.notifications.log_and_notify_error.assert_not_called()

    def test_property_service_mutations_succeed_without_zillow_env(self):
        # End-to-end assertion: with no Zillow env vars, every PropertyService
        # mutation still completes successfully and the publisher records no
        # failures (publishes are skipped at the boundary).
        from somewheria_app.services.zillow import ZillowPublisher

        zillow = ZillowPublisher(SimpleNamespace(), Mock())
        config = SimpleNamespace(
            api_base_url="https://api.example.com",
            upload_dir=Path(tempfile.gettempdir()),
        )
        service = PropertyService(config, Mock(), zillow=zillow)
        service.cache = [{"id": "prop-1", "for_sale": False, "status": "Active"}]

        with patch("somewheria_app.services.properties.requests.delete", return_value=Mock(status_code=204, text="")):
            service.delete_property("prop-1", "admin@example.com")
        service.cache = [{"id": "prop-2", "for_sale": False, "status": "Active"}]
        with patch("somewheria_app.services.properties.requests.put", return_value=Mock(status_code=200)):
            service.toggle_sale("prop-2", "admin@example.com")

        self.assertEqual(zillow.failure_count, 0)
        self.assertEqual(zillow.success_count, 0)

    def test_perform_publish_logs_when_configured(self):
        with patch.dict(
            "os.environ",
            {
                "ZILLOW_API_BASE_URL": "https://zillow.example.com",
                "ZILLOW_API_TOKEN": "token",
                "ZILLOW_FEED_KEY": "feed",
            },
        ):
            self.assertTrue(self.publisher.credentials_configured())
            # Direct call (not via the worker) avoids thread-timing flakes.
            self.publisher._perform_publish("create", "prop-1", {})
            self.publisher._record_success("create", "prop-1")
        self.assertEqual(self.publisher.success_count, 1)

    def test_retry_worker_notifies_after_max_attempts(self):
        # Force _perform_publish to fail and verify the admin alert fires
        # exactly once after MAX_ATTEMPTS attempts. Backoff is patched out so
        # the test doesn't actually wait 1+4+16 seconds.
        with patch.dict(
            "os.environ",
            {
                "ZILLOW_API_BASE_URL": "https://zillow.example.com",
                "ZILLOW_API_TOKEN": "token",
                "ZILLOW_FEED_KEY": "feed",
            },
        ), patch.object(self.publisher, "_perform_publish", side_effect=RuntimeError("nope")), patch(
            "somewheria_app.services.zillow.time.sleep", return_value=None
        ):
            self.publisher._retry_worker("create", "prop-1", {})
        self.assertEqual(self.publisher.failure_count, 1)
        self.notifications.log_and_notify_error.assert_called_once()

    def test_status_snapshot_shape(self):
        snapshot = self.publisher.status_snapshot()
        self.assertIn("configured", snapshot)
        self.assertIn("success_count", snapshot)
        self.assertIn("failure_count", snapshot)
        self.assertIn("recent_errors", snapshot)

    def test_publish_runs_inline_when_background_threads_disabled(self):
        # DISABLE_BACKGROUND_THREADS=1 mirrors the JIRA mirror's hatch:
        # publishes execute inline (no daemon thread, no backoff sleeps) so
        # tests stay deterministic and side effects are observable the
        # moment the call returns. Without this a routing test that
        # exercises a create/update/delete path with Zillow creds present
        # spawns a thread that outlives the test and races teardown.
        with patch.dict(
            "os.environ",
            {
                "ZILLOW_API_BASE_URL": "https://zillow.example.com",
                "ZILLOW_API_TOKEN": "token",
                "ZILLOW_FEED_KEY": "feed",
                "DISABLE_BACKGROUND_THREADS": "1",
            },
        ), patch.object(self.publisher, "_perform_publish") as perform:
            self.publisher.publish_create({"id": "prop-1"})
            perform.assert_called_once_with("create", "prop-1", {"property": {"id": "prop-1"}})
        # Success recorded synchronously — no thread involved.
        self.assertEqual(self.publisher.success_count, 1)
        self.assertEqual(self.publisher.failure_count, 0)

    def test_inline_publish_records_failure_and_notifies_on_error(self):
        # Same hatch, but the (stubbed) publish raises. The inline path must
        # still record the failure on the status snapshot and route the
        # admin alert through NotificationService — matching the async
        # path's contract so an operator sees the outage either way.
        with patch.dict(
            "os.environ",
            {
                "ZILLOW_API_BASE_URL": "https://zillow.example.com",
                "ZILLOW_API_TOKEN": "token",
                "ZILLOW_FEED_KEY": "feed",
                "DISABLE_BACKGROUND_THREADS": "1",
            },
        ), patch.object(
            self.publisher, "_perform_publish", side_effect=RuntimeError("boom")
        ):
            self.publisher.publish_update({"id": "prop-2"})
        self.assertEqual(self.publisher.failure_count, 1)
        self.assertEqual(self.publisher.success_count, 0)
        self.notifications.log_and_notify_error.assert_called_once()
        # The recent-errors ring buffer captured the failure for the
        # admin-status page.
        snapshot = self.publisher.status_snapshot()
        self.assertEqual(len(snapshot["recent_errors"]), 1)
        self.assertEqual(snapshot["recent_errors"][0]["action"], "update")
        self.assertEqual(snapshot["recent_errors"][0]["property_id"], "prop-2")

    def test_inline_publish_skipped_when_credentials_missing(self):
        # Even with the DISABLE_BACKGROUND_THREADS hatch on, missing
        # credentials must short-circuit before touching _perform_publish —
        # otherwise a partially-configured environment would fabricate
        # "success" for a call that was never made.
        with patch.dict(
            "os.environ",
            {
                "ZILLOW_API_BASE_URL": "",
                "ZILLOW_API_TOKEN": "",
                "ZILLOW_FEED_KEY": "",
                "DISABLE_BACKGROUND_THREADS": "1",
            },
        ), patch.object(self.publisher, "_perform_publish") as perform:
            self.publisher.publish_delete("prop-3")
            perform.assert_not_called()
        self.assertEqual(self.publisher.success_count, 0)
        self.assertEqual(self.publisher.failure_count, 0)


class TicketsNowIsoTestCase(unittest.TestCase):
    """Lock the on-disk timestamp format for tickets.

    Ticket payloads persisted to ``tickets.json`` (and the SQLite mirror) use
    ``_now_iso`` for ``created_at`` / ``updated_at`` / per-note ``at``.
    Existing fixtures and admin CSV exports assume the
    ``YYYY-MM-DDTHH:MM:SSZ`` shape — second precision, trailing ``Z``, no
    offset. Changing this would silently break downstream consumers, so we
    pin the format here.
    """

    def test_now_iso_is_seconds_precision_utc_z(self):
        import datetime as _dt
        from somewheria_app.services.tickets import _now_iso

        value = _now_iso()
        # Length is 20: "YYYY-MM-DDTHH:MM:SSZ".
        self.assertEqual(len(value), 20)
        self.assertTrue(value.endswith("Z"))
        # Parsing strips the trailing Z and yields a naive datetime equal to
        # the current UTC second.
        parsed = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        # Allow a couple of seconds of slack to avoid flakiness if the clock
        # ticks between the two reads.
        delta = abs((parsed - now_utc).total_seconds())
        self.assertLessEqual(delta, 2)


class TimeUtilTestCase(unittest.TestCase):
    """Lock the shared UTC timestamp helper.

    Everything that writes a wire-format timestamp (tickets, the JSONL change
    log, contract metadata, lead-capture submissions, …) routes through
    ``utcnow_iso``. The ``YYYY-MM-DDTHH:MM:SSZ`` shape is part of the contract
    AnalyticsTracker.recent_listing_activity relies on (it slices ``ts[:7]``
    for the month bucket).
    """

    def test_utcnow_iso_is_seconds_precision_utc_z(self):
        import datetime as _dt
        from somewheria_app.services.timeutil import utcnow_iso

        value = utcnow_iso()
        self.assertEqual(len(value), 20)
        self.assertTrue(value.endswith("Z"))
        parsed = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        self.assertLessEqual(abs((parsed - now_utc).total_seconds()), 2)

    def test_tickets_now_iso_routes_through_shared_helper(self):
        # The previous bug split timestamp generation across two helpers; if
        # tickets ever re-introduces its own implementation, this guard fails
        # loud so the change-log/recent_listing_activity bucket logic doesn't
        # silently regress.
        from somewheria_app.services import tickets, timeutil

        self.assertIs(tickets._now_iso, timeutil.utcnow_iso)


class NotificationsChangeLogConcurrencyTestCase(unittest.TestCase):
    def test_concurrent_log_site_change_writes_intact_json_lines(self):
        # Concurrent appends to change_log_file must produce whole JSONL rows,
        # not interleaved partial writes. POSIX guarantees an O_APPEND single
        # write() is atomic only up to PIPE_BUF (~4KB); a large payload
        # (e.g. properties_cache_updated with many changed properties) racing
        # a small ticket_created write can otherwise mid-line-splice into an
        # unparseable row, which analytics.recent_listing_activity silently
        # drops. The lock added in this commit is what keeps the log intact.
        import json as _json
        import threading

        with tempfile.TemporaryDirectory() as td:
            change_log = Path(td) / "site_changes.log"
            config = SimpleNamespace(change_log_file=change_log)
            service = NotificationService(config, Mock())

            big_extra = {"payload": "x" * 8000}  # pushes each line past PIPE_BUF
            small_extra = {"id": "t1"}
            threads = []
            for _ in range(20):
                threads.append(
                    threading.Thread(
                        target=service.log_site_change,
                        args=("a@example.com", "properties_cache_updated", big_extra),
                    )
                )
                threads.append(
                    threading.Thread(
                        target=service.log_site_change,
                        args=("b@example.com", "ticket_created", small_extra),
                    )
                )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = change_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 40)
            # Every line must parse as JSON with the expected action set —
            # any interleave would raise ValueError here or misroute the row.
            for line in lines:
                entry = _json.loads(line)
                self.assertIn(
                    entry["action"], ("properties_cache_updated", "ticket_created")
                )


class NotificationsChangeLogTimestampTestCase(unittest.TestCase):
    def test_log_site_change_writes_utc_z_timestamp(self):
        # Naive local-time isoformat() looks UTC-shaped but isn't, so
        # AnalyticsTracker.recent_listing_activity buckets entries by the
        # wrong calendar month when the server's local date and UTC date
        # straddle midnight. The change-log writer must emit Z-suffixed UTC.
        import json as _json

        config = SimpleNamespace(change_log_file=Path("/tmp/test_change_log.log"))
        analytics = Mock()
        service = NotificationService(config, analytics)
        handle = mock_open()
        with patch.object(Path, "open", handle):
            service.log_site_change("admin@example.com", "property_created", {"id": "p1"})

        written = "".join(call.args[0] for call in handle().write.call_args_list).strip()
        entry = _json.loads(written)
        self.assertTrue(
            entry["timestamp"].endswith("Z"),
            f"timestamp not Z-suffixed UTC: {entry['timestamp']!r}",
        )
        # ``ts[:7]`` is the YYYY-MM slice recent_listing_activity uses.
        self.assertRegex(entry["timestamp"][:7], r"^\d{4}-\d{2}$")


class EmailValidationTestCase(unittest.TestCase):
    def test_accepts_typical_addresses(self):
        for value in (
            "user@example.com",
            "first.last@example.co.uk",
            "user+tag@sub.example.com",
            "a@b.co",
            "USER@EXAMPLE.COM",
            "hyphen-name@domain-with-hyphen.io",
            "name_1@example.museum",
        ):
            self.assertTrue(is_valid_email(value), value)

    def test_rejects_obvious_junk(self):
        for value in (
            "",
            "@",
            "a@",
            "@b.com",
            "a@b",                # no TLD
            "a@b.",               # trailing dot
            "a@.b",               # leading dot in domain
            "a@b..c",             # consecutive dots
            "user @example.com",  # space in local
            "user@exa mple.com",  # space in domain
            "user@@example.com",  # multiple @
            "not-an-email",
            "user@-example.com",  # label starts with hyphen
            "user@example-.com",  # label ends with hyphen
        ):
            self.assertFalse(is_valid_email(value), value)

    def test_rejects_invalid_local_part_dots(self):
        # RFC 5321 / 5322 model the local part as a dot-atom: one or more
        # atoms separated by single dots, no empty atoms. Every real SMTP
        # server rejects the shapes below, and accepting them here made an
        # admin-approved ``.alice@example.com`` unmatchable against the
        # ``alice@example.com`` Google would actually return at login.
        for value in (
            ".alice@example.com",   # leading dot in local
            "alice.@example.com",   # trailing dot in local
            "a..b@example.com",     # consecutive dots in local
            ".@example.com",        # only-dot local
            "a...b@example.com",    # runs of dots
        ):
            self.assertFalse(is_valid_email(value), value)

    def test_rejects_non_strings(self):
        self.assertFalse(is_valid_email(None))
        self.assertFalse(is_valid_email(12345))
        self.assertFalse(is_valid_email(["a@b.com"]))
        self.assertFalse(is_valid_email({"email": "a@b.com"}))

    def test_rejects_oversized_strings(self):
        long_local = "a" * 255
        self.assertFalse(is_valid_email(f"{long_local}@example.com"))


class TicketSetEmailUpdatesTestCase(unittest.TestCase):
    """Guard the no-op short-circuit in ``TicketService.set_email_updates``.

    The route is idempotent from the user's point of view: clicking the
    toggle to the value it's already at shouldn't bump ``updated_at``
    (jumping the ticket to the top of the "recently updated" list for
    nothing) or append a spurious ``ticket_email_updates_toggled`` entry
    to ``site_changes.log`` that inflates the audit trail with noise.
    """

    def setUp(self):
        from somewheria_app.services.tickets import TicketService

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tickets_path = Path(self.tmp.name) / "tickets.json"
        self.tickets_path.write_text(
            '[{"id": "t1", "email_updates": true, "updated_at": "2020-01-01T00:00:00Z"}]',
            encoding="utf-8",
        )
        self.config = SimpleNamespace(tickets_file=self.tickets_path)
        self.storage = FileStorageService(self.config)
        self.notifications = MagicMock()
        self.service = TicketService(self.config, self.storage, self.notifications)

    def test_no_op_when_value_unchanged_preserves_updated_at(self):
        result = self.service.set_email_updates("t1", enabled=True, actor_email="a@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result["updated_at"], "2020-01-01T00:00:00Z")
        # No change-log entry when nothing actually changed.
        self.notifications.log_site_change.assert_not_called()

    def test_change_bumps_updated_at_and_logs(self):
        result = self.service.set_email_updates("t1", enabled=False, actor_email="a@example.com")
        self.assertIsNotNone(result)
        self.assertFalse(result["email_updates"])
        self.assertNotEqual(result["updated_at"], "2020-01-01T00:00:00Z")
        self.notifications.log_site_change.assert_called_once()
        args = self.notifications.log_site_change.call_args
        self.assertEqual(args[0][1], "ticket_email_updates_toggled")
        self.assertEqual(args[0][2], {"ticket_id": "t1", "enabled": False})

    def test_missing_ticket_returns_none(self):
        self.assertIsNone(
            self.service.set_email_updates("nope", enabled=True, actor_email="a@example.com")
        )
        self.notifications.log_site_change.assert_not_called()


class TicketLoadNonDictRowGuardTestCase(unittest.TestCase):
    """Regression: a stray non-dict row in tickets.json must not crash the
    ticket service.

    Everything the service writes back through ``_save`` is a dict, but a
    corrupted / hand-edited tickets.json can slip in a bare string, number,
    ``null``, or list. Every read path (``list_tickets``, ``get_ticket``,
    ``summary``, ``status_counts``, ``find_by_jira_key``) reaches into each
    entry with ``.get(...)``, which would AttributeError on a non-dict and
    take out the admin dashboard / renter dashboard / ticket routes via the
    crash handler's empty 503. Matches the isinstance(dict) guard added for
    pending registrations / lead captures in PR #146 and for the change-log
    JSONL in PR #144.
    """

    def setUp(self):
        from somewheria_app.services.tickets import TicketService

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tickets_path = Path(self.tmp.name) / "tickets.json"
        # Interleave valid ticket dicts with malformed rows the writer would
        # never emit — a bare string, a bare number, ``null``, and a list.
        self.tickets_path.write_text(
            '['
            '{"id": "t1", "status": "open", "priority": "urgent", '
            '"jira_key": "OPS-1", "submitted_by": "renter@example.com"},'
            '"garbage",'
            'null,'
            '42,'
            '["not", "a", "dict"],'
            '{"id": "t2", "status": "resolved", "priority": "normal", '
            '"submitted_by": "other@example.com"}'
            ']',
            encoding="utf-8",
        )
        self.config = SimpleNamespace(tickets_file=self.tickets_path)
        self.storage = FileStorageService(self.config)
        self.notifications = MagicMock()
        self.service = TicketService(self.config, self.storage, self.notifications)

    def test_list_tickets_skips_non_dict_rows(self):
        tickets = self.service.list_tickets()
        self.assertEqual({t["id"] for t in tickets}, {"t1", "t2"})

    def test_list_tickets_submitter_filter_skips_non_dict_rows(self):
        tickets = self.service.list_tickets(submitter="renter@example.com")
        self.assertEqual([t["id"] for t in tickets], ["t1"])

    def test_get_ticket_skips_non_dict_rows(self):
        # A ``for ticket in self._load(): ticket.get("id")`` walk that hit a
        # non-dict before finding the target would AttributeError. The guard
        # must let ``get_ticket`` reach the real row.
        self.assertEqual(self.service.get_ticket("t2")["status"], "resolved")

    def test_summary_and_status_counts_ignore_non_dict_rows(self):
        summary = self.service.summary()
        # Only the two real dicts count toward totals.
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["urgent"], 1)
        counts = self.service.status_counts()
        # Every allowed status key is zero-filled; only "open" and
        # "resolved" carry the real dicts.
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["resolved"], 1)
        self.assertEqual(counts["in_progress"], 0)

    def test_find_by_jira_key_skips_non_dict_rows(self):
        self.assertEqual(self.service.find_by_jira_key("OPS-1")["id"], "t1")


if __name__ == "__main__":
    unittest.main()
