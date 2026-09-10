import copy
import datetime
import importlib
import os
import unittest
from io import BytesIO
from unittest.mock import patch


os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

website_app = importlib.import_module("website_app")


class ExpandedRouteCoverageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        website_app.app.config.update(TESTING=True)

    def setUp(self):
        self.app = website_app.app
        self.client = self.app.test_client()
        self.services = self.app.extensions["somewheria_services"]
        with self.services.properties.cache_lock:
            self.original_cache = copy.deepcopy(self.services.properties.cache)
            self.services.properties.cache = []
        self.original_google_client_id = self.services.config.google_client_id
        self.original_google_client_secret = self.services.config.google_client_secret

    def tearDown(self):
        with self.services.properties.cache_lock:
            self.services.properties.cache = self.original_cache
        self.services.config.google_client_id = self.original_google_client_id
        self.services.config.google_client_secret = self.original_google_client_secret

    def login_as(self, role="renter", email=None, name="Test User"):
        email = email or f"{role}@example.com"
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": f"{role}-id",
                "email": email,
                "name": name,
                "role": role,
            }

    def seed_property(self, property_id="prop-1", **overrides):
        property_data = {
            "id": property_id,
            "name": "Maple House",
            "address": "123 Main St",
            "rent": "1500",
            "deposit": "1500",
            "bedrooms": "2",
            "bathrooms": "1",
            "sqft": "900",
            "lease_length": "12 months",
            "included_amenities": ["Parking", "Laundry"],
            "pets_allowed": "Yes",
            "ada_accessible": "Yes",
            "blurb": "A bright rental home.",
            "description": "Comfortable home close to transit.",
            "photos": ["https://example.com/photo1.jpg", "https://example.com/photo2.jpg"],
            "thumbnail": "https://example.com/thumb.jpg",
        }
        property_data.update(overrides)
        with self.services.properties.cache_lock:
            self.services.properties.cache = [property_data]
        return property_data

    def test_login_page_loads(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Login", response.data)

    def test_login_post_redirects_to_manage_listings(self):
        response = self.client.post("/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])

    def test_login_redirects_when_already_authenticated(self):
        self.login_as("renter")

        response = self.client.get("/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])

    def test_logout_clears_session(self):
        self.login_as("renter")

        response = self.client.get("/logout", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/", response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertNotIn("user", session)

    def test_logout_drops_every_session_key(self):
        # Logging out must not leave the CSRF token (or half-finished OAuth
        # state) alive for whoever uses the browser next.
        self.login_as("renter")
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "leftover-token"
            session["oauth_state"] = "leftover-state"

        self.client.get("/logout", follow_redirects=False)

        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})

    def test_auth_status_returns_unauthenticated_payload(self):
        response = self.client.get("/auth/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"authenticated": False, "user": None})

    def test_response_carries_request_id_header(self):
        response = self.client.get("/")
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_inbound_request_id_is_honored(self):
        response = self.client.get("/", headers={"X-Request-Id": "trace-me-123"})
        self.assertEqual(response.headers.get("X-Request-Id"), "trace-me-123")

    def test_inbound_request_id_strips_unsafe_characters(self):
        # Werkzeug's own validation blocks CR/LF in header values, but plain
        # spaces / colons / brackets pass through untouched. A raw value like
        # "spoof]: FAKE log line [rid" would otherwise land verbatim in
        # ConsoleFormatter's stdout line and let a client author fake audit
        # entries; the sanitizer drops anything outside [A-Za-z0-9._-].
        response = self.client.get(
            "/",
            headers={"X-Request-Id": "spoof]: FAKE injected [rid"},
        )

        self.assertEqual(response.status_code, 200)
        sanitized = response.headers.get("X-Request-Id", "")
        self.assertTrue(sanitized)
        for banned in (" ", ":", "[", "]"):
            self.assertNotIn(banned, sanitized)
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        )
        self.assertTrue(all(ch in allowed for ch in sanitized))

    def test_inbound_request_id_fully_unsafe_falls_back_to_generated(self):
        # If every character is stripped (spaces / punctuation only), the
        # sanitized value is empty and we must mint a fresh id — an empty
        # X-Request-Id header would leave downstream log tooling with nothing
        # to correlate against.
        response = self.client.get("/", headers={"X-Request-Id": "   :::   "})

        self.assertEqual(response.status_code, 200)
        sanitized = response.headers.get("X-Request-Id", "")
        self.assertTrue(sanitized)
        for banned in (" ", ":"):
            self.assertNotIn(banned, sanitized)

    def test_request_id_sanitizer_helper(self):
        # Direct unit coverage for the helper — Werkzeug's test client refuses
        # CR/LF in outbound headers, so the log-injection payload we most
        # actually care about (a newline followed by a forged log line) can
        # only be exercised at the function level, not through the client.
        from somewheria_app import _sanitize_request_id

        self.assertEqual(_sanitize_request_id("abc-123"), "abc-123")
        self.assertEqual(_sanitize_request_id("safe.value_1"), "safe.value_1")
        # Newline + forged log line: every unsafe char is stripped.
        self.assertEqual(
            _sanitize_request_id("hack\n[FAKE] injected"),
            "hackFAKEinjected",
        )
        # Empty / non-string inputs collapse to "" (caller then generates one).
        self.assertEqual(_sanitize_request_id(""), "")
        self.assertEqual(_sanitize_request_id(None), "")
        # Enforce the 64-char cap so a hostile client can't bloat log lines.
        self.assertEqual(len(_sanitize_request_id("a" * 200)), 64)

    def test_auth_status_returns_authenticated_payload(self):
        self.login_as("renter", email="renter@example.com", name="Renter User")

        response = self.client.get("/auth/status")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["email"], "renter@example.com")
        self.assertEqual(payload["user"]["name"], "Renter User")

    def test_for_rent_page_loads(self):
        self.seed_property()

        with patch.object(self.services.properties, "refresh_cache"):
            response = self.client.get("/for-rent")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maple House", response.data)

    def test_manage_listings_loads_when_logged_in(self):
        self.login_as("admin", email="admin@example.com")
        self.seed_property()

        response = self.client.get("/manage-listings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Manage Listings", response.data)
        self.assertIn(b"123 Main St", response.data)

    def test_for_rent_json_returns_cached_properties(self):
        self.seed_property(included_amenities={"Parking", "Laundry"})

        with patch.object(self.services.properties, "refresh_cache"):
            response = self.client.get("/for-rent.json")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "Maple House")
        self.assertCountEqual(payload[0]["included_amenities"], ["Parking", "Laundry"])

    def test_for_rent_serves_cached_properties_when_upstream_is_down(self):
        # Regression: a transient upstream outage used to blank the
        # listing page because ``_fetch_property_ids`` swallowed the
        # exception and ``refresh_cache`` then overwrote the cache with
        # an empty list. Now the upstream failure propagates as
        # ``UpstreamUnavailable``, the route's try/except catches it,
        # and the previously-cached properties remain visible.
        from somewheria_app.services.properties import UpstreamUnavailable

        self.seed_property()
        with patch.object(
            self.services.properties,
            "refresh_cache",
            side_effect=UpstreamUnavailable("upstream down"),
        ):
            response = self.client.get("/for-rent")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maple House", response.data)

    def test_for_rent_json_serves_cached_properties_when_upstream_is_down(self):
        from somewheria_app.services.properties import UpstreamUnavailable

        self.seed_property()
        with patch.object(
            self.services.properties,
            "refresh_cache",
            side_effect=UpstreamUnavailable("upstream down"),
        ):
            response = self.client.get("/for-rent.json")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "Maple House")

    def test_for_rent_refresh_uses_anonymous_actor_when_logged_out(self):
        with patch.object(self.services.properties, "trigger_background_refresh") as trigger_mock:
            response = self.client.get("/for-rent-refresh.json")

        self.assertEqual(response.status_code, 200)
        trigger_mock.assert_called_once_with("anonymous")

    def test_for_rent_refresh_uses_logged_in_email(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "trigger_background_refresh") as trigger_mock:
            response = self.client.get("/for-rent-refresh.json")

        self.assertEqual(response.status_code, 200)
        trigger_mock.assert_called_once_with("admin@example.com")

    def test_property_details_renders_existing_property(self):
        self.seed_property()
        with patch.object(self.services.appointments, "load", return_value={"prop-1": {"2030-01-10"}}):
            response = self.client.get("/property/prop-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maple House", response.data)
        self.assertIn(b"ADA accessible", response.data)
        self.assertIn(b"Yes", response.data)

    def test_property_details_returns_404_when_property_missing(self):
        # Not in cache AND the direct fetch finds nothing -> 404.
        with patch.object(self.services.properties, "fetch_property_record", return_value=None):
            response = self.client.get("/property/missing-prop")

        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Property not found", response.data)

    def test_property_details_falls_back_to_direct_fetch_on_cold_cache(self):
        # Cache empty (e.g. right after a restart), but a direct fetch finds
        # the listing — the page must render, not 404, so crawlers/shared
        # links hitting the URL directly still work.
        record = {
            "id": "prop-9",
            "name": "Cold Cache Cottage",
            "address": "9 Restart Rd",
            "rent": "1200",
            "bedrooms": "1",
            "bathrooms": "1",
            "sqft": "600",
            "lease_length": "12 months",
            "pets_allowed": "No",
        }
        with patch.object(self.services.properties, "get_property", return_value=None), patch.object(
            self.services.properties, "fetch_property_record", return_value=record
        ) as fetch_mock, patch.object(self.services.appointments, "load", return_value={}):
            response = self.client.get("/property/prop-9")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cold Cache Cottage", response.data)
        fetch_mock.assert_called_once_with("prop-9")

    def test_schedule_appointment_rejects_invalid_date(self):
        response = self.client.post(
            "/property/prop-1/schedule",
            json={
                "name": "Alex",
                "date": "not-a-date",
                "contact_method": "email",
                "contact_info": "alex@example.com",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Invalid date.")

    def test_schedule_appointment_rejects_past_date(self):
        past_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

        response = self.client.post(
            "/property/prop-1/schedule",
            json={
                "name": "Alex",
                "date": past_date,
                "contact_method": "email",
                "contact_info": "alex@example.com",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Date cannot be in the past.")

    def test_schedule_appointment_rejects_far_future_date(self):
        too_far = (datetime.date.today() + datetime.timedelta(days=400)).isoformat()

        response = self.client.post(
            "/property/prop-1/schedule",
            json={
                "name": "Alex",
                "date": too_far,
                "contact_method": "email",
                "contact_info": "alex@example.com",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("within", response.get_json()["error"])

    def test_schedule_appointment_returns_404_when_property_is_missing(self):
        future_date = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        with patch.object(self.services.properties, "fetch_live_property_name", return_value=None):
            response = self.client.post(
                "/property/missing-prop/schedule",
                json={
                    "name": "Alex",
                    "date": future_date,
                    "contact_method": "email",
                    "contact_info": "alex@example.com",
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Property not found.")

    def test_schedule_appointment_success_sends_email(self):
        future_date = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        with patch.object(self.services.properties, "fetch_live_property_name", return_value="Maple House"), patch.object(
            self.services.appointments, "book", return_value=True
        ), patch.object(
            self.services.notifications,
            "send_email",
        ) as send_email_mock:
            response = self.client.post(
                "/property/prop-1/schedule",
                json={
                    "name": "Alex",
                    "date": future_date,
                    "contact_method": "email",
                    "contact_info": "alex@example.com",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True})
        send_email_mock.assert_called_once()
        self.assertIn("Viewing Appointment Request", send_email_mock.call_args[0][0])
        self.assertIn("Maple House", send_email_mock.call_args[0][1])

    def test_schedule_appointment_persists_booking(self):
        future_date = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        with patch.object(self.services.properties, "fetch_live_property_name", return_value="Maple House"), patch.object(
            self.services.appointments, "book", return_value=True
        ) as book_mock, patch.object(self.services.notifications, "send_email"):
            response = self.client.post(
                "/property/prop-1/schedule",
                json={
                    "name": "Alex",
                    "date": future_date,
                    "contact_method": "email",
                    "contact_info": "alex@example.com",
                },
            )

        self.assertEqual(response.status_code, 200)
        book_mock.assert_called_once_with("prop-1", future_date)

    def test_schedule_appointment_rejects_double_booking(self):
        future_date = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        with patch.object(self.services.properties, "fetch_live_property_name", return_value="Maple House"), patch.object(
            self.services.appointments, "book", return_value=False
        ), patch.object(self.services.notifications, "send_email") as send_email_mock:
            response = self.client.post(
                "/property/prop-1/schedule",
                json={
                    "name": "Alex",
                    "date": future_date,
                    "contact_method": "email",
                    "contact_info": "alex@example.com",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "That date is already booked.")
        send_email_mock.assert_not_called()

    def test_about_page_loads(self):
        response = self.client.get("/about")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"About", response.data)

    def test_contact_page_loads(self):
        response = self.client.get("/contact")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contact", response.data)

    def test_contact_submit_requires_name_email_and_message(self):
        for payload in (
            {"name": "", "email": "a@example.com", "message": "Hi"},
            {"name": "Jamie", "email": "not-an-email", "message": "Hi"},
            {"name": "Jamie", "email": "a@example.com", "message": ""},
        ):
            with patch.object(
                self.services.notifications, "send_email_async"
            ) as async_mock, patch.object(
                self.services.notifications, "send_email"
            ) as sync_mock:
                response = self.client.post("/contact", data=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn(
                b"Name, a valid email, and a message are required.", response.data
            )
            async_mock.assert_not_called()
            sync_mock.assert_not_called()

    def test_contact_submit_dispatches_email_off_request_thread(self):
        # send_email blocks up to SMTP_TIMEOUT_SECONDS (30 s); the async
        # wrapper keeps this unauthenticated POST from being an easy way to
        # pin a gunicorn worker on a slow Gmail relay. Pin the wrapper so a
        # future refactor can't quietly regress it back onto the request
        # thread.
        with patch.object(
            self.services.notifications, "send_email_async"
        ) as async_mock, patch.object(
            self.services.notifications, "send_email"
        ) as sync_mock:
            response = self.client.post(
                "/contact",
                data={
                    "name": "Jamie",
                    "email": "jamie@example.com",
                    "message": "Interested in a viewing.",
                },
            )

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_not_called()
        async_mock.assert_called_once()
        self.assertEqual(async_mock.call_args.args[0], "Contact Form Message")
        body = async_mock.call_args.args[1]
        self.assertIn("Jamie", body)
        self.assertIn("jamie@example.com", body)
        self.assertIn("Interested in a viewing.", body)

    def test_logs_page_loads(self):
        self.login_as("high_admin")
        with patch.object(self.services.notifications, "read_logs", return_value=[]):
            response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Logger", response.data)

    def test_report_issue_form_loads(self):
        response = self.client.get("/report-issue")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Report", response.data)

    def test_report_issue_complete_loads_confirmation_page(self):
        response = self.client.get("/report-issue-complete")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Report", response.data)

    def test_report_issue_requires_name_and_description(self):
        response = self.client.post("/report-issue", data={"name": "", "description": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Name and description are required fields.", response.data)

    def test_report_issue_sends_email_and_renders_confirmation(self):
        with patch.object(self.services.notifications, "send_email") as send_email_mock:
            response = self.client.post(
                "/report-issue",
                data={"name": "Jamie", "description": "Broken contact form"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Jamie", response.data)
        send_email_mock.assert_called_once()
        self.assertIn("User Reported Issue", send_email_mock.call_args[0][0])

    def test_register_page_loads(self):
        response = self.client.get("/register")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Register", response.data)

    def test_register_requires_name_and_email(self):
        response = self.client.post("/register", data={"name": "", "email": ""})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Name and a valid email are required.", response.data)

    def test_register_rejects_malformed_email(self):
        for garbage in ("@", "a@", "@b.com", "a@b"):
            with patch.object(self.services.storage, "add_pending_registration") as add_mock, patch.object(
                self.services.notifications, "send_email"
            ) as send_email_mock:
                response = self.client.post(
                    "/register",
                    data={"name": "Jamie", "email": garbage, "reason": "Need access"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Name and a valid email are required.", response.data)
            add_mock.assert_not_called()
            send_email_mock.assert_not_called()

    def test_register_saves_pending_registration_and_sends_email(self):
        with patch.object(self.services.storage, "get_pending_registrations", return_value=[]), patch.object(
            self.services.storage,
            "add_pending_registration",
        ) as add_pending_mock, patch.object(
            self.services.notifications,
            "send_email",
        ) as send_email_mock:
            response = self.client.post(
                "/register",
                data={"name": "Jamie", "email": "jamie@example.com", "reason": "Need access"},
            )

        self.assertEqual(response.status_code, 200)
        add_pending_mock.assert_called_once_with(
            {"name": "Jamie", "email": "jamie@example.com", "reason": "Need access"}
        )
        send_email_mock.assert_called_once()

    def test_register_honeypot_drops_submission_silently(self):
        # A filled honeypot field means a bot; the route must show the normal
        # success page while storing and notifying nothing.
        with patch.object(self.services.storage, "add_pending_registration") as add_mock, patch.object(
            self.services.notifications, "send_email"
        ) as send_email_mock:
            response = self.client.post(
                "/register",
                data={
                    "name": "Jamie",
                    "email": "jamie@example.com",
                    "reason": "Need access",
                    "website": "http://spam.example",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Request received", response.data)
        add_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_register_drops_link_spam_silently(self):
        # URLs, punycode hosts, and the comment-spam "hs=" marker in the name
        # or reason mark the submission as spam.
        for spam_reason in (
            "Confirm your transaction https://xn--example.xn--p1ai/?x",
            "hs=483e1cf023c07655365eb0529961c86b",
            "visit www.spam.example now",
        ):
            with patch.object(self.services.storage, "add_pending_registration") as add_mock, patch.object(
                self.services.notifications, "send_email"
            ) as send_email_mock:
                response = self.client.post(
                    "/register",
                    data={"name": "Jamie", "email": "jamie@example.com", "reason": spam_reason},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Request received", response.data)
            add_mock.assert_not_called()
            send_email_mock.assert_not_called()

    def test_register_dispatches_admin_email_off_request_thread(self):
        # The anonymous /register POST must not block on the SMTP relay --
        # send_email_async wraps a background thread (or an inline shim under
        # DISABLE_BACKGROUND_THREADS=1) so a slow Gmail can't stall the
        # response for up to SMTP_TIMEOUT_SECONDS. Pin the wrapper is used
        # so a future refactor can't quietly regress the request thread.
        with patch.object(self.services.storage, "get_pending_registrations", return_value=[]), patch.object(
            self.services.storage,
            "add_pending_registration",
        ), patch.object(
            self.services.notifications,
            "send_email_async",
        ) as async_mock:
            response = self.client.post(
                "/register",
                data={"name": "Jamie", "email": "jamie@example.com", "reason": "Need access"},
            )

        self.assertEqual(response.status_code, 200)
        async_mock.assert_called_once()
        self.assertEqual(async_mock.call_args.args[0], "New Registration Request")

    def test_register_duplicate_does_not_send_email(self):
        # When storage reports the email is already pending (returns False),
        # the route must not fire a second admin notification.
        with patch.object(
            self.services.storage, "add_pending_registration", return_value=False
        ) as add_pending_mock, patch.object(
            self.services.notifications,
            "send_email",
        ) as send_email_mock:
            response = self.client.post(
                "/register",
                data={"name": "Jamie", "email": "jamie@example.com", "reason": "Need access"},
            )

        self.assertEqual(response.status_code, 200)
        add_pending_mock.assert_called_once()
        send_email_mock.assert_not_called()

    def test_admin_registrations_page_loads_for_admin(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_pending_registrations", return_value=[]):
            response = self.client.get("/admin/registrations")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pending", response.data)

    def test_admin_registrations_requires_email_on_post(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_pending_registrations", return_value=[]), patch.object(
            self.services.storage,
            "set_user_role",
        ) as set_role_mock, patch.object(self.services.storage, "remove_pending_registration") as remove_pending_mock, patch.object(
            self.services.notifications,
            "send_email",
        ) as send_email_mock:
            response = self.client.post("/admin/registrations", data={"action": "approve", "email": ""})

        self.assertEqual(response.status_code, 200)
        set_role_mock.assert_not_called()
        remove_pending_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_admin_registrations_rejects_invalid_action(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            return_value=[{"email": "pending@example.com", "name": "Pending"}],
        ), patch.object(self.services.storage, "set_user_role") as set_role_mock, patch.object(
            self.services.storage,
            "remove_pending_registration",
        ) as remove_pending_mock, patch.object(self.services.notifications, "send_email") as send_email_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "wat", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        set_role_mock.assert_not_called()
        remove_pending_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_admin_registrations_approve_calls_storage_and_email(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(self.services.storage, "set_user_role") as set_role_mock, patch.object(
            self.services.storage,
            "remove_pending_registration",
        ) as remove_pending_mock, patch.object(self.services.notifications, "send_email") as send_email_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "approve", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        set_role_mock.assert_called_once_with("pending@example.com", "renter")
        remove_pending_mock.assert_called_once_with("pending@example.com")
        send_email_mock.assert_called_once()
        # The approval email must go to the user being approved, not the admin inbox.
        self.assertEqual(send_email_mock.call_args.kwargs.get("to"), "pending@example.com")

    def test_admin_registrations_reject_calls_storage_and_email(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(self.services.storage, "remove_pending_registration") as remove_pending_mock, patch.object(
            self.services.notifications,
            "send_email",
        ) as send_email_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "reject", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        remove_pending_mock.assert_called_once_with("pending@example.com")
        send_email_mock.assert_called_once()
        # The rejection email must go to the user being rejected, not the admin inbox.
        self.assertEqual(send_email_mock.call_args.kwargs.get("to"), "pending@example.com")

    def test_admin_registrations_approve_rejects_email_that_is_not_pending(self):
        # A hand-crafted POST (or a stale tab racing another admin's decision)
        # must not be able to grant the renter role to an address that was
        # never vetted through the registration flow.
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            return_value=[{"email": "pending@example.com", "name": "Pending"}],
        ), patch.object(self.services.storage, "set_user_role") as set_role_mock, patch.object(
            self.services.storage,
            "remove_pending_registration",
        ) as remove_pending_mock, patch.object(self.services.notifications, "send_email") as send_email_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "approve", "email": "never-applied@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        set_role_mock.assert_not_called()
        remove_pending_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_admin_registrations_reject_rejects_email_that_is_not_pending(self):
        # Same guard on the reject path: nobody who never applied should
        # receive a rejection email.
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            return_value=[{"email": "pending@example.com", "name": "Pending"}],
        ), patch.object(
            self.services.storage, "remove_pending_registration"
        ) as remove_pending_mock, patch.object(
            self.services.notifications, "send_email"
        ) as send_email_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "reject", "email": "never-applied@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        remove_pending_mock.assert_not_called()
        send_email_mock.assert_not_called()

    def test_admin_registrations_approve_dispatches_email_off_request_thread(self):
        # The approve/reject admin action must not block on the SMTP relay --
        # send_email_async keeps the response snappy even when Gmail stalls
        # right up to SMTP_TIMEOUT_SECONDS. Pin the wrapper is used on both
        # branches so a future refactor can't quietly regress the request
        # thread back to a 30 s hang per approval.
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(self.services.storage, "set_user_role"), patch.object(
            self.services.storage, "remove_pending_registration"
        ), patch.object(
            self.services.notifications,
            "send_email_async",
        ) as async_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "approve", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        async_mock.assert_called_once()
        self.assertEqual(async_mock.call_args.args[0], "Registration Approved")
        self.assertEqual(async_mock.call_args.kwargs.get("to"), "pending@example.com")

    def test_admin_registrations_reject_dispatches_email_off_request_thread(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(
            self.services.storage, "remove_pending_registration"
        ), patch.object(
            self.services.notifications, "send_email_async"
        ) as async_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "reject", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        async_mock.assert_called_once()
        self.assertEqual(async_mock.call_args.args[0], "Registration Rejected")
        self.assertEqual(async_mock.call_args.kwargs.get("to"), "pending@example.com")

    def test_admin_registrations_approve_writes_audit_entry(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(self.services.storage, "set_user_role"), patch.object(
            self.services.storage, "remove_pending_registration"
        ), patch.object(self.services.notifications, "send_email"), patch.object(
            self.services.notifications, "log_site_change"
        ) as log_change_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "approve", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        log_change_mock.assert_called_once()
        self.assertEqual(log_change_mock.call_args.args[1], "registration_approved")
        self.assertEqual(
            log_change_mock.call_args.args[2], {"email": "pending@example.com"}
        )

    def test_admin_registrations_reject_writes_audit_entry(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_pending_registrations",
            side_effect=[
                [{"email": "pending@example.com", "name": "Pending"}],
                [],
            ],
        ), patch.object(
            self.services.storage, "remove_pending_registration"
        ), patch.object(self.services.notifications, "send_email"), patch.object(
            self.services.notifications, "log_site_change"
        ) as log_change_mock:
            response = self.client.post(
                "/admin/registrations",
                data={"action": "reject", "email": "pending@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        log_change_mock.assert_called_once()
        self.assertEqual(log_change_mock.call_args.args[1], "registration_rejected")

    def test_admin_users_page_loads(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_user_roles", return_value={"admin@example.com": "admin"}):
            response = self.client.get("/admin/users")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"User Management", response.data)

    def test_admin_users_requires_email_on_post(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_user_roles", return_value={}):
            response = self.client.post("/admin/users", data={"email": "", "role": "renter", "action": "update"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No email provided.", response.data)

    def test_admin_users_delete_missing_user_shows_error(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "delete_user_role", return_value=False), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ):
            response = self.client.post(
                "/admin/users",
                data={"email": "missing@example.com", "action": "delete"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"User not found.", response.data)

    def test_admin_users_update_role_succeeds(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "set_user_role") as set_user_role_mock, patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"user@example.com": "renter"},
        ):
            response = self.client.post(
                "/admin/users",
                data={"email": "user@example.com", "role": "renter", "action": "update"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"updated to renter", response.data)
        set_user_role_mock.assert_called_once_with("user@example.com", "renter")

    def test_admin_users_update_role_writes_audit_log(self):
        # Mirrors the user_role_updated entry the /admin/dashboard form emits.
        # Without this, role changes made through /admin/users leave no record
        # in site_changes.log -- so the regression test pins the audit trail
        # for both code paths.
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.storage, "set_user_role"), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"user@example.com": "renter"},
        ), patch.object(self.services.notifications, "log_site_change") as log_mock:
            response = self.client.post(
                "/admin/users",
                data={"email": "user@example.com", "role": "renter", "action": "update"},
            )

        self.assertEqual(response.status_code, 200)
        log_mock.assert_called_once_with(
            "admin@example.com",
            "user_role_updated",
            {"email": "user@example.com", "role": "renter"},
        )

    def test_admin_users_delete_writes_audit_log(self):
        # Same gap as the update path: /admin/users delete used to mutate
        # user_roles without emitting a user_deleted audit entry. Pin both
        # the user-facing message and the structured log call.
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.storage, "delete_user_role", return_value=True), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ), patch.object(self.services.notifications, "log_site_change") as log_mock:
            response = self.client.post(
                "/admin/users",
                data={"email": "renter@example.com", "action": "delete"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Deactivated", response.data)
        log_mock.assert_called_once_with(
            "admin@example.com",
            "user_deleted",
            {"email": "renter@example.com"},
        )

    def test_admin_users_delete_missing_user_does_not_log(self):
        # A delete that hits a non-existent email leaves storage untouched, so
        # the audit trail must NOT pretend a delete happened -- otherwise
        # site_changes.log would record phantom removals every time a typo
        # missed an account.
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.storage, "delete_user_role", return_value=False), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ), patch.object(self.services.notifications, "log_site_change") as log_mock:
            response = self.client.post(
                "/admin/users",
                data={"email": "ghost@example.com", "action": "delete"},
            )

        self.assertEqual(response.status_code, 200)
        log_mock.assert_not_called()

    def test_admin_users_role_change_rejected_for_peer_does_not_log(self):
        # A standard admin attempting to assign another admin role must be
        # rejected -- and the rejected attempt must NOT show up in the audit
        # log as a successful role change.
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.storage, "set_user_role") as set_user_role_mock, patch.object(
            self.services.auth, "get_user_role", return_value="admin"
        ), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"peer@example.com": "admin"},
        ), patch.object(self.services.notifications, "log_site_change") as log_mock:
            response = self.client.post(
                "/admin/users",
                data={"email": "peer@example.com", "role": "admin", "action": "update"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You cannot assign a role", response.data)
        set_user_role_mock.assert_not_called()
        log_mock.assert_not_called()

    def test_admin_users_high_admin_can_promote_to_high_admin(self):
        # The strict rank comparison used to make high_admin unassignable by
        # ANYONE through the UI (3 > 3 is false even for a high_admin actor),
        # so the top role could only ever be granted via .env. High admins
        # must be able to promote a lower-ranked user to their own rank.
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(self.services.storage, "set_user_role") as set_user_role_mock, patch.object(
            self.services.auth, "get_user_role", return_value="admin"
        ), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"tester@example.com": "admin"},
        ), patch.object(self.services.notifications, "log_site_change") as log_mock:
            response = self.client.post(
                "/admin/users",
                data={"email": "tester@example.com", "role": "high_admin", "action": "update"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"updated to high_admin", response.data)
        set_user_role_mock.assert_called_once_with("tester@example.com", "high_admin")
        log_mock.assert_called_once_with(
            "owner@example.com",
            "user_role_updated",
            {"email": "tester@example.com", "role": "high_admin"},
        )

    def test_toggle_active_deactivates_listing_and_redirects(self):
        self.login_as("admin", email="admin@example.com")
        self.seed_property("prop-1")
        with patch.object(self.services.properties, "is_listing_hidden", return_value=False), patch.object(
            self.services.properties, "set_listing_active"
        ) as set_active_mock:
            response = self.client.post("/toggle-active/prop-1")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])
        set_active_mock.assert_called_once_with("prop-1", active=False, actor_email="admin@example.com")

    def test_toggle_active_reactivates_hidden_listing(self):
        self.login_as("admin", email="admin@example.com")
        self.seed_property("prop-1")
        with patch.object(self.services.properties, "is_listing_hidden", return_value=True), patch.object(
            self.services.properties, "set_listing_active"
        ) as set_active_mock:
            response = self.client.post("/toggle-active/prop-1")

        self.assertEqual(response.status_code, 302)
        set_active_mock.assert_called_once_with("prop-1", active=True, actor_email="admin@example.com")

    def test_toggle_active_rejected_for_renter(self):
        self.login_as("renter")
        response = self.client.post("/toggle-active/prop-1")

        self.assertEqual(response.status_code, 403)

    def test_for_rent_excludes_deactivated_listings(self):
        self.seed_property("prop-1")
        with patch.object(self.services.properties, "refresh_cache"), patch.object(
            self.services.properties, "hidden_listing_ids", return_value={"prop-1"}
        ):
            response = self.client.get("/for-rent")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Maple House", response.data)

    def test_property_details_hidden_returns_404_for_public(self):
        self.seed_property("prop-1")
        with patch.object(self.services.properties, "is_listing_hidden", return_value=True):
            response = self.client.get("/property/prop-1")

        self.assertEqual(response.status_code, 404)

    def test_property_details_hidden_still_visible_to_admin(self):
        self.login_as("admin")
        self.seed_property("prop-1")
        with patch.object(self.services.properties, "is_listing_hidden", return_value=True), patch.object(
            self.services.appointments, "load", return_value={}
        ):
            response = self.client.get("/property/prop-1")

        self.assertEqual(response.status_code, 200)

    def test_admin_users_rejects_malformed_email_on_role_assignment(self):
        # Defense-in-depth check: an admin pasting a typo'd value like "not-an-email"
        # should be rejected at the route boundary before user_roles storage is
        # touched. Without this gate, garbage entries accumulate in user_roles.json
        # that no real OAuth login can ever match.
        self.login_as("admin")
        with patch.object(self.services.storage, "set_user_role") as set_user_role_mock, patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ):
            response = self.client.post(
                "/admin/users",
                data={"email": "not-an-email", "role": "renter", "action": "update"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"valid email is required", response.data)
        set_user_role_mock.assert_not_called()

    def test_admin_dashboard_forbids_standard_admin(self):
        self.login_as("admin")

        response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access restricted", response.data)

    def test_admin_dashboard_loads_for_high_admin(self):
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(self.services.analytics, "dashboard_data", return_value=({"visits": 10}, {"labels": []})), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"owner@example.com": "high_admin"},
        ):
            response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin Dashboard", response.data)

    def test_admin_dashboard_adds_user_for_high_admin(self):
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(self.services.analytics, "dashboard_data", return_value=({"visits": 10}, {"labels": []})), patch.object(
            self.services.storage,
            "get_user_roles",
            side_effect=[{}, {"new@example.com": "admin"}],
        ), patch.object(self.services.storage, "set_user_role") as set_user_role_mock, patch.object(
            self.services.notifications,
            "log_site_change",
        ) as log_site_change_mock:
            response = self.client.post(
                "/admin/dashboard",
                data={"action": "add", "email": "new@example.com", "role": "admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"added as admin", response.data)
        set_user_role_mock.assert_called_once_with("new@example.com", "admin")
        log_site_change_mock.assert_called_once()

    def test_admin_dashboard_rejects_malformed_email_on_add(self):
        # Mirror of test_admin_users_rejects_malformed_email_on_role_assignment for
        # the combined admin dashboard's "add user" path. A typo'd email must not
        # reach user_roles storage.
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(self.services.analytics, "dashboard_data", return_value=({"visits": 10}, {"labels": []})), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ), patch.object(self.services.storage, "set_user_role") as set_user_role_mock:
            response = self.client.post(
                "/admin/dashboard",
                data={"action": "add", "email": "not-an-email", "role": "admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"valid email is required", response.data)
        set_user_role_mock.assert_not_called()

    def test_admin_dashboard_rejects_malformed_email_on_update(self):
        # Defense in depth for the ``update`` action: without this guard, a
        # hand-crafted POST with a garbage email like "not-an-email" would
        # silently write a role entry that no real OAuth login can match,
        # polluting user_roles storage. Parity with the ``add`` action above
        # and with ``admin_users`` (test_admin_users_rejects_malformed_email_on_role_assignment).
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(self.services.analytics, "dashboard_data", return_value=({"visits": 10}, {"labels": []})), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={},
        ), patch.object(self.services.storage, "set_user_role") as set_user_role_mock:
            response = self.client.post(
                "/admin/dashboard",
                data={"action": "update", "email": "not-an-email", "role": "admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"valid email is required", response.data)
        set_user_role_mock.assert_not_called()

    def test_admin_dashboard_excludes_revoked_users_from_summary(self):
        # A "revoked" tombstone is a deleted user kept only so an env role
        # can't silently restore access on the next login. Passing it into
        # the dashboard "users" list would count it as a renter (the
        # template's role tally is admin/high_admin/else, so any other role
        # — including "revoked" — falls into the renter bucket) and inflate
        # the total user count. The route must strip revoked entries.
        self.login_as("high_admin", email="owner@example.com")
        stored_roles = {
            "active_renter@example.com": "renter",
            "deleted@example.com": "revoked",
        }
        with patch.object(
            self.services.analytics,
            "dashboard_data",
            return_value=({"visits": 0}, {"labels": []}),
        ), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value=stored_roles,
        ), patch(
            "somewheria_app.routes.admin_routes.render_template",
            return_value="ok",
        ) as render_mock:
            response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 200)
        kwargs = render_mock.call_args.kwargs
        users = kwargs["users"]
        self.assertEqual(users, [("active_renter@example.com", "renter")])
        for _, role in users:
            self.assertNotEqual(role, "revoked")

    def test_admin_status_excludes_revoked_users_from_known_users_count(self):
        # Mirrors the dashboard fix: /admin/status's "known_users" metric
        # is the size of the persistent user_roles map, but revoked
        # tombstones represent deleted users and must not be counted.
        self.login_as("high_admin", email="owner@example.com")
        stored_roles = {
            "one@example.com": "renter",
            "two@example.com": "admin",
            "revoked1@example.com": "revoked",
            "revoked2@example.com": "revoked",
        }
        with patch.object(
            self.services.storage,
            "get_user_roles",
            return_value=stored_roles,
        ), patch.object(
            self.services.storage,
            "get_pending_registrations",
            return_value=[],
        ), patch(
            "somewheria_app.routes.admin_routes.render_template",
            return_value="ok",
        ) as render_mock:
            response = self.client.get("/admin/status")

        self.assertEqual(response.status_code, 200)
        kwargs = render_mock.call_args.kwargs
        self.assertEqual(kwargs["metrics"]["known_users"], 2)

    def test_renter_dashboard_loads_for_renter(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(
            self.services.storage,
            "get_renter_contracts",
            return_value={"renter@example.com": [{"property_name": "Maple House"}]},
        ):
            response = self.client.get("/renter-dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maple House", response.data)

    def test_renter_dashboard_backfill_preserves_concurrent_contract_writes(self):
        """The id-backfill path must not overwrite a newer version of the
        renter's contract list that an admin write installed between the
        initial (unlocked) read and the persist-inside-``atomic()`` call.

        Regression guard: prior code saved the pre-lock snapshot back,
        silently dropping any contract added or removed in between.
        """
        self.login_as("renter", email="renter@example.com")
        # Pre-lock snapshot: one contract without an ``id`` so the backfill
        # actually persists a write.
        original_snapshot = {
            "renter@example.com": [{"property_name": "Old House"}],
        }
        # Fresh state observed inside the storage lock: the same renter now
        # also has a brand-new contract added concurrently by an admin.
        fresh_state = {
            "renter@example.com": [
                {"property_name": "Old House"},
                {"property_name": "New House", "id": "existing-id"},
            ],
        }
        # ``get_renter_contracts`` is called twice — first pre-lock, then
        # again inside the atomic() to re-read the fresh list. Return the
        # stale snapshot first and the fresh state second.
        calls = {"n": 0}

        def _get_contracts():
            calls["n"] += 1
            return original_snapshot if calls["n"] == 1 else fresh_state

        with patch.object(
            self.services.storage,
            "get_renter_contracts",
            side_effect=_get_contracts,
        ), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_mock:
            response = self.client.get("/renter-dashboard")

        self.assertEqual(response.status_code, 200)
        # The concurrent contract must have survived the backfill.
        save_mock.assert_called_once()
        saved = save_mock.call_args[0][0]
        saved_names = [c.get("property_name") for c in saved.get("renter@example.com", [])]
        self.assertIn("Old House", saved_names)
        self.assertIn("New House", saved_names)

    def test_renter_profile_loads_existing_profile(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(
            self.services.storage,
            "get_renter_profiles",
            return_value={"renter@example.com": {"name": "Jamie", "contact": "555-0100"}},
        ):
            response = self.client.get("/renter/profile")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Jamie", response.data)

    def test_renter_profile_post_saves_profile(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(self.services.storage, "get_renter_profiles", return_value={}), patch.object(
            self.services.storage,
            "save_renter_profiles",
        ) as save_profiles_mock:
            response = self.client.post(
                "/renter/profile",
                data={"name": "Jamie", "contact": "555-0100"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Profile updated.", response.data)
        # The profile now carries a ticket-email preference; an unchecked box
        # submits nothing, which the route interprets as False.
        save_profiles_mock.assert_called_once_with(
            {
                "renter@example.com": {
                    "name": "Jamie",
                    "contact": "555-0100",
                    "email_status_updates": False,
                    "rcs_status_updates": False,
                }
            }
        )

    def test_renter_profile_post_honors_email_status_updates_checkbox(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(self.services.storage, "get_renter_profiles", return_value={}), patch.object(
            self.services.storage,
            "save_renter_profiles",
        ) as save_profiles_mock:
            response = self.client.post(
                "/renter/profile",
                data={"name": "Jamie", "contact": "555-0100", "email_status_updates": "1"},
            )

        self.assertEqual(response.status_code, 200)
        save_profiles_mock.assert_called_once_with(
            {
                "renter@example.com": {
                    "name": "Jamie",
                    "contact": "555-0100",
                    "email_status_updates": True,
                    "rcs_status_updates": False,
                }
            }
        )

    def test_admin_contracts_add_requires_all_fields(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}):
            response = self.client.post(
                "/admin/contracts",
                data={"action": "add", "renter_email": "", "property_name": "", "start_date": "", "end_date": ""},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"All fields are required.", response.data)

    def test_admin_contracts_add_rejects_malformed_renter_email(self):
        # A typo like "renter@" must not be persisted: it would create an
        # orphan contract that no real OAuth login can ever surface.
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@",
                    "property_name": "Maple House",
                    "start_date": "2030-01-01",
                    "end_date": "2030-12-31",
                    "status": "Active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"valid renter email is required", response.data)
        save_contracts_mock.assert_not_called()

    def test_admin_contracts_add_successfully_saves(self):
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2030-01-01",
                    "end_date": "2030-12-31",
                    "status": "Active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contract added for renter@example.com.", response.data)
        save_contracts_mock.assert_called_once()

    def test_admin_contracts_add_rejects_malformed_dates(self):
        # A hand-crafted POST (or a paste-error) with a non-ISO date must be
        # rejected at the boundary so garbage never lands in
        # renter_contracts storage. Downstream _classify_contract_status
        # silently swallows parse failures, so without this the admin sees
        # the literal junk string rendered as the contract's term.
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2030-13-40",  # invalid month/day
                    "end_date": "2030-12-31",
                    "status": "Active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"YYYY-MM-DD", response.data)
        save_contracts_mock.assert_not_called()

    def test_admin_contracts_add_rejects_non_extended_iso_shapes(self):
        # ``datetime.date.fromisoformat`` on 3.11+ accepts extra ISO 8601
        # shapes that happen to be 10 characters long — the week date
        # ``2030-W12-1`` (parses as 2030-03-18) and the basic form with
        # trailing garbage ``"20301212XX"`` (parses as 2030-12-12) — so a
        # length-only check would let them into renter_contracts storage.
        # The admin dashboard would then render the literal junk string as
        # the contract's term. Reject both at the boundary.
        self.login_as("admin")
        for bad in ("2030-W12-1", "20301212XX"):
            with patch.object(
                self.services.storage, "get_renter_contracts", return_value={}
            ), patch.object(
                self.services.storage, "save_renter_contracts"
            ) as save_contracts_mock:
                response = self.client.post(
                    "/admin/contracts",
                    data={
                        "action": "add",
                        "renter_email": "renter@example.com",
                        "property_name": "Maple House",
                        "start_date": bad,
                        "end_date": "2030-12-31",
                        "status": "Active",
                    },
                )
                self.assertEqual(response.status_code, 200, msg=bad)
                self.assertIn(b"YYYY-MM-DD", response.data, msg=bad)
                save_contracts_mock.assert_not_called()

    def test_admin_contracts_add_rejects_end_before_start(self):
        # Contracts whose end precedes their start are nonsense and would
        # classify as "ended" the moment they're saved. The most common
        # cause is a year/month swap the admin should be told to fix.
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2030-12-31",
                    "end_date": "2030-01-01",
                    "status": "Active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"End date must not be before start date.", response.data)
        save_contracts_mock.assert_not_called()

    def test_admin_contracts_add_accepts_same_start_and_end(self):
        # A one-day contract is unusual but valid; end >= start (not strict >)
        # is the correct predicate.
        self.login_as("admin")
        with patch.object(self.services.storage, "get_renter_contracts", return_value={}), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2030-06-15",
                    "end_date": "2030-06-15",
                    "status": "Active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contract added for renter@example.com.", response.data)
        save_contracts_mock.assert_called_once()

    def test_admin_contracts_delete_rejects_invalid_index(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_renter_contracts",
            return_value={"renter@example.com": [{"property_name": "Maple House"}]},
        ):
            response = self.client.post(
                "/admin/contracts",
                data={"action": "delete", "renter_email": "renter@example.com", "contract_index": "abc"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid contract index.", response.data)

    def test_admin_contracts_delete_successfully_saves(self):
        self.login_as("admin")
        contracts = {"renter@example.com": [{"property_name": "Maple House"}]}
        with patch.object(self.services.storage, "get_renter_contracts", return_value=contracts), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ) as save_contracts_mock:
            response = self.client.post(
                "/admin/contracts",
                data={"action": "delete", "renter_email": "renter@example.com", "contract_index": "0"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contract removed for renter@example.com.", response.data)
        save_contracts_mock.assert_called_once_with({})

    def test_admin_contracts_delete_rejects_traversal_pdf_filename(self):
        self.login_as("admin")
        contracts = {
            "renter@example.com": [
                {"property_name": "Maple House", "pdf_filename": "../../../etc/passwd"}
            ]
        }
        with patch.object(self.services.storage, "get_renter_contracts", return_value=contracts), patch.object(
            self.services.storage,
            "save_renter_contracts",
        ), patch.object(self.services.storage, "delete_file") as delete_file_mock:
            response = self.client.post(
                "/admin/contracts",
                data={"action": "delete", "renter_email": "renter@example.com", "contract_index": "0"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Contract removed for renter@example.com.", response.data)
        # The traversal filename must never reach the filesystem layer.
        delete_file_mock.assert_not_called()

    def test_analytics_dashboard_forbids_standard_admin(self):
        self.login_as("admin")

        response = self.client.get("/admin/analytics")

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access restricted", response.data)

    def test_analytics_dashboard_loads_for_high_admin(self):
        self.login_as("high_admin")
        with patch.object(self.services.analytics, "dashboard_data", return_value=({"visits": 10}, {"labels": []})):
            response = self.client.get("/admin/analytics")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Site Analytics", response.data)

    def test_add_listing_loads_for_admin(self):
        self.login_as("admin")

        response = self.client.get("/add-listing")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Edit your listing", response.data)

    def test_edit_listing_returns_404_when_missing(self):
        self.login_as("admin")

        response = self.client.get("/edit-listing/missing")

        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Property not found", response.data)

    def test_edit_listing_loads_existing_property_for_admin(self):
        self.login_as("admin")
        self.seed_property()

        response = self.client.get("/edit-listing/prop-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"123 Main St", response.data)

    def test_save_edit_creates_new_property_and_redirects(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "create_property") as create_property_mock:
            response = self.client.post("/save-edit/new", data={"name": "Maple House"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])
        create_property_mock.assert_called_once()

    def test_save_edit_updates_existing_property_and_redirects(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "update_property") as update_property_mock:
            response = self.client.post("/save-edit/prop-1", data={"name": "Maple House"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])
        update_property_mock.assert_called_once()

    def test_save_edit_returns_404_when_property_update_is_missing(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "update_property", side_effect=KeyError("Property not found")):
            response = self.client.post("/save-edit/prop-1", data={"name": "Maple House"})

        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Property not found", response.data)

    def test_delete_listing_redirects_when_successful(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "delete_property") as delete_property_mock:
            response = self.client.post("/delete-listing/prop-1", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])
        delete_property_mock.assert_called_once_with("prop-1", "admin@example.com")

    def test_upload_image_requires_file_part(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.notifications, "log_and_notify_error") as notify_mock:
            response = self.client.post("/upload-image/prop-1", data={}, content_type="multipart/form-data")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        notify_mock.assert_called_once()

    def test_upload_image_requires_selected_filename(self):
        self.login_as("admin", email="admin@example.com")
        data = {"file": (BytesIO(b""), "")}
        with patch.object(self.services.notifications, "log_and_notify_error") as notify_mock:
            response = self.client.post("/upload-image/prop-1", data=data, content_type="multipart/form-data")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        notify_mock.assert_called_once()

    def test_image_edit_notify_success(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.notifications, "notify_image_edit") as notify_mock:
            response = self.client.post("/image-edit-notify", json={"images": ["https://example.com/a.jpg"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Notification sent.")
        notify_mock.assert_called_once_with(["(See admin console for details.)"])

    def test_image_edit_notify_handles_failure(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(
            self.services.notifications,
            "notify_image_edit",
            side_effect=RuntimeError("boom"),
        ), patch.object(self.services.notifications, "log_and_notify_error") as log_error_mock:
            response = self.client.post("/image-edit-notify", json={"images": ["https://example.com/a.jpg"]})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], "Failed to send notification.")
        log_error_mock.assert_called_once()

    def test_toggle_sale_returns_404_for_missing_property(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "toggle_sale", side_effect=KeyError("Property not found")):
            response = self.client.post("/toggle-sale/prop-1")

        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Property not found", response.data)

    def test_toggle_sale_redirects_when_successful(self):
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "toggle_sale") as toggle_sale_mock:
            response = self.client.post("/toggle-sale/prop-1", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage-listings", response.headers["Location"])
        toggle_sale_mock.assert_called_once_with("prop-1", "admin@example.com")

    def test_property_routes_do_not_double_log_site_change(self):
        # The property service layer already calls log_site_change for create,
        # update, delete, toggle-sale, and image upload. The route layer used
        # to call it a second time, inflating site_changes.log and
        # double-counting created/deleted events in recent_listing_activity
        # (which feeds the admin dashboard chart). Guard against the
        # regression by verifying no log_site_change calls originate from the
        # routes themselves when the service methods are mocked out.
        self.login_as("admin", email="admin@example.com")
        with patch.object(self.services.properties, "create_property"), patch.object(
            self.services.properties, "update_property"
        ), patch.object(self.services.properties, "delete_property"), patch.object(
            self.services.properties, "toggle_sale"
        ), patch.object(
            self.services.properties, "upload_image", return_value="/static/uploads/x.jpg"
        ), patch.object(
            self.services.notifications, "log_site_change"
        ) as log_mock:
            self.client.post("/save-edit/new", data={"name": "Maple"})
            self.client.post("/save-edit/prop-1", data={"name": "Maple"})
            self.client.post("/delete-listing/prop-1")
            self.client.post("/toggle-sale/prop-1")
            self.client.post(
                "/upload-image/prop-1",
                data={"file": (BytesIO(b"image"), "photo.png")},
                content_type="multipart/form-data",
            )

        log_mock.assert_not_called()

    def test_google_callback_shows_oauth_error_screen_when_not_configured(self):
        self.services.config.google_client_id = ""
        self.services.config.google_client_secret = ""

        response = self.client.get("/google/callback")

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Google Sign-In", response.data)

    def test_google_login_redirects_when_oauth_is_configured(self):
        self.services.config.google_client_id = "client-id"
        self.services.config.google_client_secret = "client-secret"
        flow_mock = type("FlowMock", (), {})()
        flow_mock.redirect_uri = None
        flow_mock.authorization_url = lambda **kwargs: ("https://accounts.google.com/o/oauth2/auth?mock=1", "state-123")

        with patch("somewheria_app.routes.auth_routes.Flow.from_client_config", return_value=flow_mock):
            response = self.client.get("/google/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("https://accounts.google.com/o/oauth2/auth?mock=1", response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertEqual(session["oauth_state"], "state-123")

    def test_offline_page_loads(self):
        response = self.client.get("/offline")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Offline", response.data)

    def test_manifest_json_is_served(self):
        response = self.client.get("/manifest.json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/manifest+json", response.content_type)

    def test_service_worker_is_served_with_no_cache_header(self):
        response = self.client.get("/service-worker.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")

    def test_robots_txt_allows_search_engines_and_claude(self):
        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.content_type)
        body = response.data.decode()
        # Search engines + Claude explicitly allowed; everything else blocked.
        for agent in ("Googlebot", "Bingbot", "ClaudeBot"):
            self.assertIn(f"User-agent: {agent}", body)
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)

    def test_google_site_verification_file_is_served_at_root(self):
        response = self.client.get("/google425c45881532a134.html")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"google-site-verification: google425c45881532a134.html", response.data)

    def test_home_has_meta_description(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<meta name="description" content="', response.data)
        self.assertIn(b"family-run", response.data)

    def test_home_has_open_graph_and_favicon(self):
        response = self.client.get("/")

        body = response.data
        self.assertIn(b'<meta property="og:title"', body)
        self.assertIn(b'<meta property="og:image"', body)
        self.assertIn(b'<meta name="twitter:card" content="summary_large_image"', body)
        self.assertIn(b'rel="icon"', body)

    def test_favicon_ico_is_served(self):
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertIn("image/png", response.content_type)

    def test_for_rent_includes_map_scripts(self):
        # Regression: the script block was previously outside {% block content %}
        # so Jinja discarded it, breaking the map view.
        self.seed_property()
        with patch.object(self.services.properties, "refresh_cache"):
            response = self.client.get("/for-rent")

        body = response.data
        self.assertIn(b"propertyMap", body)
        # The inline initializer (not just the Leaflet <script src>) is present.
        self.assertIn(b"L.tileLayer", body)

    def test_sitemap_lists_public_pages_and_properties(self):
        self.seed_property()
        with patch.object(self.services.properties, "refresh_cache"):
            response = self.client.get("/sitemap.xml")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/xml", response.content_type)
        body = response.data.decode()
        self.assertIn("<urlset", body)
        self.assertIn("/for-rent", body)
        # The seeded property's detail URL is present.
        self.assertIn("/property/prop-1", body)

    def test_property_details_omits_tour_embed_when_url_blank(self):
        self.seed_property(tour_url="")
        with patch.object(self.services.appointments, "load", return_value={}):
            response = self.client.get("/property/prop-1")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="tour-embed"', response.data)
        self.assertNotIn(b"View 3D tour", response.data)
        self.assertNotIn(b"<iframe", response.data)

    def test_property_details_renders_tour_embed_when_url_present(self):
        self.seed_property(tour_url="https://my.matterport.com/show/?m=abc")
        with patch.object(self.services.appointments, "load", return_value={}):
            response = self.client.get("/property/prop-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="tour-embed"', response.data)
        self.assertIn(b"View 3D tour", response.data)
        self.assertIn(b"https://my.matterport.com/show/?m=abc", response.data)
        self.assertIn(b"<iframe", response.data)

    # ------------------------------------------------------------ reporting

    def test_admin_dashboard_includes_reporting_chart_data_for_high_admin(self):
        self.login_as("high_admin", email="owner@example.com")
        with patch.object(
            self.services.analytics,
            "dashboard_data",
            return_value=({"visits": 10}, {"labels": []}),
        ), patch.object(
            self.services.analytics,
            "recent_listing_activity",
            return_value={"months": ["2026-04", "2026-05"], "created": [1, 2], "deleted": [0, 1]},
        ), patch.object(
            self.services.tickets,
            "status_counts",
            return_value={"open": 3, "closed": 1, "in_progress": 0, "awaiting_parts": 0, "resolved": 0},
        ), patch.object(
            self.services.storage,
            "get_user_roles",
            return_value={"owner@example.com": "high_admin"},
        ):
            response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tickets by status", response.data)
        self.assertIn(b"Listings by month", response.data)

    def test_admin_dashboard_forbids_renter_for_chart_view(self):
        self.login_as("renter")

        response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 403)

    def test_admin_contracts_export_csv_returns_csv_for_admin(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_renter_contracts",
            return_value={
                "renter@example.com": [
                    {
                        "property_name": "Maple House",
                        "start_date": "2030-01-01",
                        "end_date": "2030-12-31",
                        "status": "Active",
                        "created_at": "2030-01-01T00:00:00",
                    }
                ]
            },
        ):
            response = self.client.get("/admin/contracts/export.csv")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        body = response.get_data(as_text=True)
        self.assertIn("renter_email,property_name,start_date,end_date,status,created_at", body)
        self.assertIn("renter@example.com", body)
        self.assertIn("Maple House", body)

    def test_admin_contracts_export_csv_forbids_renter(self):
        self.login_as("renter")

        response = self.client.get("/admin/contracts/export.csv")

        self.assertEqual(response.status_code, 403)

    def test_admin_tickets_export_csv_returns_csv_for_admin(self):
        self.login_as("admin")
        sample_ticket = {
            "id": "abc123",
            "title": "Leaky faucet",
            "status": "open",
            "priority": "normal",
            "category": "plumbing",
            "submitted_by": "renter@example.com",
            "property_name": "Maple House",
            "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-02T00:00:00Z",
        }
        with patch.object(self.services.tickets, "list_tickets", return_value=[sample_ticket]):
            response = self.client.get("/admin/tickets/export.csv")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        body = response.get_data(as_text=True)
        self.assertIn("id,title,status,priority,category,submitter,property_name,created_at,last_updated", body)
        self.assertIn("Leaky faucet", body)
        self.assertIn("renter@example.com", body)

    def test_admin_tickets_export_csv_forbids_renter(self):
        self.login_as("renter")

        response = self.client.get("/admin/tickets/export.csv")

        self.assertEqual(response.status_code, 403)

    def test_admin_ticket_update_returns_404_when_ticket_missing(self):
        # The other ticket routes (detail, add-note, toggle-email) already
        # 404 on a miss. Before this fix admin_ticket_update silently
        # redirected to a broken detail page whenever the ticket had been
        # deleted or the id was mistyped, hiding the mistake from admins.
        self.login_as("admin")

        with patch.object(
            self.services.tickets, "update_ticket", return_value=None
        ) as mock_update:
            response = self.client.post(
                "/admin/tickets/nonexistent-id",
                data={"status": "in_progress"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 404)
        mock_update.assert_called_once()

    def test_admin_ticket_update_redirects_on_success(self):
        self.login_as("admin")

        sample_ticket = {
            "id": "abc123",
            "title": "Leaky",
            "status": "in_progress",
            "priority": "high",
        }
        with patch.object(
            self.services.tickets, "update_ticket", return_value=sample_ticket
        ):
            response = self.client.post(
                "/admin/tickets/abc123",
                data={"status": "in_progress"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/tickets/abc123", response.headers["Location"])

    def test_admin_contracts_export_csv_neutralizes_formula_injection(self):
        # A malicious admin or hand-edited storage could record a property
        # name beginning with `=` (or `+`, `-`, `@`). When the resulting CSV
        # is opened in Excel / LibreOffice / Google Sheets the leading
        # character is interpreted as the start of a formula. The export
        # must defuse those cells by prefixing with a single quote.
        self.login_as("admin")
        with patch.object(
            self.services.storage,
            "get_renter_contracts",
            return_value={
                "=cmd|'/c calc'!A1": [
                    {
                        "property_name": "=HYPERLINK(\"http://evil\")",
                        "start_date": "+1",
                        "end_date": "-1",
                        "status": "@SUM(1+1)",
                        "created_at": "2030-01-01T00:00:00",
                    }
                ]
            },
        ):
            response = self.client.get("/admin/contracts/export.csv")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        data_line = body.splitlines()[1]
        # Each formula-triggering cell must be prefixed with a single quote.
        # The renter_email cell contains a comma so csv.writer will quote it;
        # check the escaped substring instead of the whole field.
        self.assertIn("'=cmd|'/c calc'!A1", data_line)
        self.assertIn("'=HYPERLINK", data_line)
        self.assertIn("'+1", data_line)
        self.assertIn("'-1", data_line)
        self.assertIn("'@SUM(1+1)", data_line)
        # Non-triggering cells stay untouched.
        self.assertIn("2030-01-01T00:00:00", data_line)

    def test_admin_tickets_export_csv_neutralizes_formula_injection(self):
        # Any user who can file a ticket can choose its title, which lands
        # verbatim in the admin CSV export. Without escaping, a title like
        # ``=cmd|'/c calc'!A1`` becomes an executable formula on open.
        self.login_as("admin")
        sample_ticket = {
            "id": "abc123",
            "title": "=cmd|'/c calc'!A1",
            "status": "open",
            "priority": "normal",
            "category": "plumbing",
            "submitted_by": "+renter@example.com",
            "property_name": "@SUM(1+1)",
            "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-02T00:00:00Z",
        }
        with patch.object(self.services.tickets, "list_tickets", return_value=[sample_ticket]):
            response = self.client.get("/admin/tickets/export.csv")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        data_line = body.splitlines()[1]
        self.assertIn("'=cmd|'/c calc'!A1", data_line)
        self.assertIn("'+renter@example.com", data_line)
        self.assertIn("'@SUM(1+1)", data_line)
        # Benign cells are unchanged.
        self.assertIn("abc123", data_line)
        self.assertIn("open", data_line)


    def test_for_rent_renders_filter_bar_and_map(self):
        self.seed_property()
        with patch.object(self.services.properties, "refresh_cache"):
            response = self.client.get("/for-rent")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"filterBar", response.data)
        self.assertIn(b"propertyMap", response.data)
        # CSP should permit Leaflet from unpkg.com and tiles from
        # *.tile.openstreetmap.org for the §3.4 map view.
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("https://unpkg.com", csp)
        self.assertIn("tile.openstreetmap.org", csp)
        self.assertIn("nominatim.openstreetmap.org", csp)

    # ---------------- Phase 3 §1 — renter portal additions ----------------

    @staticmethod
    def _png_bytes(size=(10, 10)):
        """Return a minimal in-memory PNG for upload tests."""
        from PIL import Image as _Image

        buffer = BytesIO()
        _Image.new("RGB", size, color=(120, 200, 80)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_admin_contracts_add_with_pdf_persists_filename_and_id(self):
        self.login_as("admin")
        captured = {}

        def fake_save(target, data):
            captured["path"] = target
            captured["bytes"] = data
            return True

        with patch.object(
            self.services.storage, "get_renter_contracts", return_value={}
        ), patch.object(
            self.services.storage, "save_renter_contracts"
        ) as save_mock, patch.object(
            self.services.storage, "save_binary_file", side_effect=fake_save
        ) as save_binary_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                    "status": "Active",
                    "contract_pdf": (BytesIO(b"%PDF-1.4 hello"), "lease.pdf"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        save_binary_mock.assert_called_once()
        save_mock.assert_called_once()
        saved_payload = save_mock.call_args[0][0]
        contracts_for_renter = saved_payload["renter@example.com"]
        self.assertEqual(len(contracts_for_renter), 1)
        contract = contracts_for_renter[0]
        self.assertEqual(contract["property_name"], "Maple House")
        self.assertTrue(contract["id"])
        self.assertTrue(contract["pdf_filename"].endswith(".pdf"))
        self.assertIn(contract["id"], contract["pdf_filename"])
        self.assertEqual(captured["bytes"], b"%PDF-1.4 hello")

    def test_admin_contracts_rejects_non_pdf_upload(self):
        self.login_as("admin")
        with patch.object(
            self.services.storage, "get_renter_contracts", return_value={}
        ), patch.object(
            self.services.storage, "save_renter_contracts"
        ) as save_mock, patch.object(
            self.services.storage, "save_binary_file"
        ) as save_binary_mock:
            response = self.client.post(
                "/admin/contracts",
                data={
                    "action": "add",
                    "renter_email": "renter@example.com",
                    "property_name": "Maple House",
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                    "status": "Active",
                    "contract_pdf": (BytesIO(b"not really a pdf"), "fake.pdf"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not a valid PDF", response.data)
        save_binary_mock.assert_not_called()
        save_mock.assert_not_called()

    def test_contract_detail_renter_can_view_own_contract(self):
        self.login_as("renter", email="renter@example.com")
        contracts_data = {
            "renter@example.com": [
                {
                    "id": "contract-abc",
                    "property_name": "Maple House",
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                    "status": "Active",
                    "pdf_filename": "",
                }
            ]
        }
        with patch.object(
            self.services.storage, "get_renter_contracts", return_value=contracts_data
        ):
            response = self.client.get("/contracts/contract-abc")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Maple House", response.data)

    def test_contract_detail_404_when_renter_does_not_own(self):
        self.login_as("renter", email="renter@example.com")
        contracts_data = {
            "someone-else@example.com": [
                {
                    "id": "contract-xyz",
                    "property_name": "Other House",
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                    "status": "Active",
                    "pdf_filename": "",
                }
            ]
        }
        with patch.object(
            self.services.storage, "get_renter_contracts", return_value=contracts_data
        ):
            response = self.client.get("/contracts/contract-xyz")
        self.assertEqual(response.status_code, 404)

    def test_backfill_contract_ids_does_not_persist_status_class(self):
        """``status_class`` is derived from today's date and the contract's
        start/end dates, so persisting it lets a "pending" contract keep
        rendering as pending long after start_date has passed. The backfill
        must add ids to disk without leaking the derived field.
        """
        from somewheria_app.routes.admin_routes import _backfill_contract_ids

        # Contracts missing ``id`` — triggers the backfill save.
        contracts = [
            {
                "property_name": "Maple House",
                "start_date": "2024-01-01",
                "end_date": "2025-01-01",
                "status": "unrecognized-freeform",
            }
        ]
        # Snapshot the payload AT SAVE TIME (deep copy) because the function
        # mutates the same dict in place after the save call to attach the
        # derived ``status_class`` — a plain Mock only records references.
        saved_snapshots: list[dict] = []

        def _snapshot_save(payload):
            saved_snapshots.append(copy.deepcopy(payload))

        with patch.object(
            self.services.storage, "get_renter_contracts", return_value={"renter@example.com": contracts}
        ), patch.object(
            self.services.storage, "save_renter_contracts", side_effect=_snapshot_save
        ):
            _backfill_contract_ids(self.services, contracts, "renter@example.com")

        self.assertEqual(len(saved_snapshots), 1)
        saved_payload = saved_snapshots[0]
        for saved_contract in saved_payload["renter@example.com"]:
            self.assertNotIn(
                "status_class",
                saved_contract,
                "status_class is derived from today's date; persisting it stales the badge",
            )
            self.assertIn("id", saved_contract, "id should have been backfilled")
        # In-memory, the returned contracts still carry the derived field
        # so callers (dashboard render) can use it without an extra call.
        self.assertIn("status_class", contracts[0])

    def test_contract_detail_recomputes_stale_status_class(self):
        """A ``status_class`` persisted by an earlier backfill (before the
        fix) can become stale as dates advance; the detail route must
        recompute it rather than trust the stored value.
        """
        self.login_as("renter", email="renter@example.com")
        # start_date is in the past — the fresh classification is "active".
        # The persisted value is deliberately stale ("pending"), matching
        # what a pre-fix backfill would have written to disk.
        yesterday = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()
        contracts_data = {
            "renter@example.com": [
                {
                    "id": "stale-status",
                    "property_name": "Maple House",
                    "start_date": yesterday,
                    "end_date": future,
                    "status": "unrecognized-freeform",
                    "status_class": "pending",  # stale, persisted from before the fix
                    "pdf_filename": "",
                }
            ]
        }
        with patch.object(
            self.services.storage, "get_renter_contracts", return_value=contracts_data
        ):
            response = self.client.get("/contracts/stale-status")
        self.assertEqual(response.status_code, 200)
        # The active badge picks up ``sw-badge-ok``; a stale pending would
        # render ``sw-badge-warn`` per the template's mapping.
        self.assertIn(b"sw-badge-ok", response.data)
        self.assertNotIn(b"sw-badge-warn", response.data)

    def test_contract_download_serves_pdf(self):
        self.login_as("renter", email="renter@example.com")
        contract_id = "contract-dl"
        upload_dir = self.services.config.contract_upload_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = upload_dir / f"{contract_id}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 sample")
        contracts_data = {
            "renter@example.com": [
                {
                    "id": contract_id,
                    "property_name": "Maple House",
                    "start_date": "2024-01-01",
                    "end_date": "2025-01-01",
                    "status": "Active",
                    "pdf_filename": f"{contract_id}.pdf",
                }
            ]
        }
        try:
            with patch.object(
                self.services.storage, "get_renter_contracts", return_value=contracts_data
            ):
                response = self.client.get(f"/contracts/{contract_id}/download")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/pdf")
            self.assertTrue(response.data.startswith(b"%PDF"))
        finally:
            try:
                pdf_path.unlink()
            except OSError:
                pass

    def test_contract_download_404_when_unknown(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(
            self.services.storage, "get_renter_contracts", return_value={}
        ):
            response = self.client.get("/contracts/does-not-exist/download")
        self.assertEqual(response.status_code, 404)

    def test_contract_pdfs_stored_outside_static_tree(self):
        """Regression: contract PDFs must not live under ``static/`` because
        Flask's static handler would serve them at /static/uploads/contracts/...
        with no authentication, bypassing the @renter_required check on
        /contracts/<id>/download."""
        config = self.services.config
        contract_dir = config.contract_upload_dir.resolve()
        static_dir = config.static_dir.resolve()
        self.assertFalse(
            str(contract_dir).startswith(str(static_dir) + os.sep)
            or contract_dir == static_dir,
            f"Contract upload directory {contract_dir} sits under the "
            f"static folder {static_dir}; this exposes signed PDFs via "
            f"Flask's static handler with no auth check.",
        )

    def test_static_url_does_not_serve_unauth_contract_pdf(self):
        """An unauthenticated request to a /static URL that mirrors the old
        contract path must NOT return a PDF that exists in the contract
        store. Pairs with test_contract_pdfs_stored_outside_static_tree
        and catches a regression if someone moves the dir back under static/."""
        contract_id = "regression-leak-uuid"
        pdf_name = f"{contract_id}.pdf"
        pdf_path = self.services.config.contract_upload_dir / pdf_name
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4 regression-secret")
        try:
            # No login. The /static URL the old code used must not serve it.
            response = self.client.get(f"/static/uploads/contracts/{pdf_name}")
            self.assertNotEqual(response.status_code, 200)
            self.assertNotIn(b"regression-secret", response.data)
        finally:
            try:
                pdf_path.unlink()
            except OSError:
                pass

    def test_renter_profile_post_persists_rcs_status_updates(self):
        self.login_as("renter", email="renter@example.com")
        with patch.object(
            self.services.storage, "get_renter_profiles", return_value={}
        ), patch.object(
            self.services.storage, "save_renter_profiles"
        ) as save_mock:
            response = self.client.post(
                "/renter/profile",
                data={
                    "name": "Jamie",
                    "contact": "555-0100",
                    "email_status_updates": "1",
                    "rcs_status_updates": "1",
                },
            )
        self.assertEqual(response.status_code, 200)
        save_mock.assert_called_once_with(
            {
                "renter@example.com": {
                    "name": "Jamie",
                    "contact": "555-0100",
                    "email_status_updates": True,
                    "rcs_status_updates": True,
                }
            }
        )

    def test_ticket_new_form_includes_photo_input(self):
        response = self.client.get("/tickets/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="photos"', response.data)
        self.assertIn(b"multipart/form-data", response.data)

    def test_ticket_service_add_photo_persists_image(self):
        from somewheria_app.services.tickets import MAX_TICKET_PHOTOS

        self.login_as("renter", email="renter@example.com")
        # Create a ticket through the service so storage holds it.
        services = self.services
        # Patch out emails so the service doesn't try SMTP.
        with patch.object(services.notifications, "send_email", return_value=True):
            ticket = services.tickets.create_ticket(
                {
                    "title": "Leak",
                    "description": "Water under the sink",
                    "category": "plumbing",
                    "priority": "high",
                },
                "renter@example.com",
            )
        try:
            png = self._png_bytes()

            class _Upload:
                def __init__(self, name, blob):
                    self.filename = name
                    self.stream = BytesIO(blob)

            updated = services.tickets.add_photo(
                ticket["id"], _Upload("leak.png", png), "renter@example.com"
            )
            self.assertIsNotNone(updated)
            self.assertEqual(len(updated["photos"]), 1)
            url = updated["photos"][0]["url"]
            self.assertTrue(url.startswith(f"/static/uploads/tickets/{ticket['id']}/"))
            on_disk = self.services.config.base_dir / url.lstrip("/").replace("/", os.sep)
            self.assertTrue(on_disk.exists())

            # Limit guard: cannot exceed MAX_TICKET_PHOTOS.
            for _ in range(MAX_TICKET_PHOTOS - 1):
                services.tickets.add_photo(
                    ticket["id"], _Upload("more.png", self._png_bytes()), "renter@example.com"
                )
            from somewheria_app.services.properties import UploadValidationError

            with self.assertRaises(UploadValidationError):
                services.tickets.add_photo(
                    ticket["id"], _Upload("over.png", self._png_bytes()), "renter@example.com"
                )
        finally:
            # Best-effort cleanup of test artifacts.
            ticket_dir = self.services.config.ticket_upload_dir / ticket["id"]
            if ticket_dir.exists():
                for child in ticket_dir.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    ticket_dir.rmdir()
                except OSError:
                    pass
            # Remove the ticket from tickets.json so other tests don't see it.
            tickets_file = self.services.config.tickets_file
            if tickets_file.exists():
                try:
                    tickets_file.unlink()
                except OSError:
                    pass

    def test_ticket_service_add_photo_rejects_non_image(self):
        from somewheria_app.services.properties import UploadValidationError

        services = self.services
        with patch.object(services.notifications, "send_email", return_value=True):
            ticket = services.tickets.create_ticket(
                {
                    "title": "Light",
                    "description": "Bulb out",
                    "category": "electrical",
                    "priority": "low",
                },
                "renter@example.com",
            )
        try:

            class _Upload:
                filename = "evil.png"
                stream = BytesIO(b"this is not really a png")

            with self.assertRaises(UploadValidationError):
                services.tickets.add_photo(
                    ticket["id"], _Upload(), "renter@example.com"
                )
        finally:
            tickets_file = self.services.config.tickets_file
            if tickets_file.exists():
                try:
                    tickets_file.unlink()
                except OSError:
                    pass

    def test_ticket_service_add_photo_cleans_orphan_on_save_failure(self):
        """If persisting the ticket JSON fails after the photo is on disk,
        the orphaned image file must be removed so static/uploads/tickets/
        doesn't accumulate dead bytes on every save error."""
        from somewheria_app.services.properties import UploadValidationError

        services = self.services
        with patch.object(services.notifications, "send_email", return_value=True):
            ticket = services.tickets.create_ticket(
                {
                    "title": "Outlet",
                    "description": "Sparks",
                    "category": "electrical",
                    "priority": "urgent",
                },
                "renter@example.com",
            )
        ticket_dir = self.services.config.ticket_upload_dir / ticket["id"]
        try:

            class _Upload:
                def __init__(self, name, blob):
                    self.filename = name
                    self.stream = BytesIO(blob)

            with patch.object(
                services.tickets, "_save", side_effect=OSError("disk full")
            ):
                with self.assertRaises(UploadValidationError):
                    services.tickets.add_photo(
                        ticket["id"],
                        _Upload("orphan.png", self._png_bytes()),
                        "renter@example.com",
                    )

            # The photo must NOT remain on disk after a failed save.
            if ticket_dir.exists():
                remaining = list(ticket_dir.iterdir())
                self.assertEqual(
                    remaining,
                    [],
                    f"expected ticket upload dir to be empty, found {remaining}",
                )
        finally:
            if ticket_dir.exists():
                for child in ticket_dir.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    ticket_dir.rmdir()
                except OSError:
                    pass
            tickets_file = self.services.config.tickets_file
            if tickets_file.exists():
                try:
                    tickets_file.unlink()
                except OSError:
                    pass


class LazyFileStatusTestCase(unittest.TestCase):
    def test_existing_file_reports_present(self):
        import tempfile
        from pathlib import Path

        from somewheria_app.routes.admin_routes import _lazy_file_status

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roles.json"
            path.write_text("{}")
            status = _lazy_file_status("User Roles File", path)
            self.assertTrue(status["ok"])
            self.assertEqual(status["detail"], "Present")

    def test_absent_file_in_writable_dir_is_healthy(self):
        # A lazily-created file that doesn't exist yet is a normal state on a
        # fresh deploy, not a fault.
        import tempfile
        from pathlib import Path

        from somewheria_app.routes.admin_routes import _lazy_file_status

        with tempfile.TemporaryDirectory() as tmp:
            status = _lazy_file_status("User Roles File", Path(tmp) / "roles.json")
            self.assertTrue(status["ok"])
            self.assertEqual(status["detail"], "Not created yet")

    def test_absent_file_in_nonexistent_dir_is_flagged(self):
        from pathlib import Path

        from somewheria_app.routes.admin_routes import _lazy_file_status

        status = _lazy_file_status(
            "User Roles File", Path("/no/such/dir/really/roles.json")
        )
        self.assertFalse(status["ok"])
        self.assertIn("not writable", status["detail"])


class PropertyMetaDescriptionTestCase(unittest.TestCase):
    """Search-snippet builder is capped at Google's ~158-char display width.

    Both the blurb-derived path AND the fact-derived fallback must respect
    the cap: a listing without a blurb whose name and address are moderately
    long would otherwise render a 200+ char meta description that gets cut
    mid-word in search results.
    """

    def _describe(self, **prop):
        from somewheria_app.routes.public_routes import (
            _META_DESCRIPTION_MAX,
            _property_meta_description,
        )

        return _property_meta_description(prop), _META_DESCRIPTION_MAX

    def test_short_blurb_passes_through_unchanged(self):
        desc, _ = self._describe(blurb="Sunlit two-bedroom near the park.")
        self.assertEqual(desc, "Sunlit two-bedroom near the park.")

    def test_blurb_collapses_internal_whitespace(self):
        desc, _ = self._describe(blurb="Sunlit  two-bedroom\nnear the park.")
        self.assertEqual(desc, "Sunlit two-bedroom near the park.")

    def test_blurb_at_cap_is_not_truncated(self):
        blurb = "x" * 158
        desc, cap = self._describe(blurb=blurb)
        self.assertEqual(desc, blurb)
        self.assertEqual(len(desc), cap)

    def test_long_blurb_is_truncated_with_ellipsis(self):
        blurb = "x" * 400
        desc, cap = self._describe(blurb=blurb)
        self.assertLessEqual(len(desc), cap)
        self.assertTrue(desc.endswith("…"))

    def test_description_used_when_blurb_missing(self):
        desc, _ = self._describe(description="Quiet corner unit with parking.")
        self.assertEqual(desc, "Quiet corner unit with parking.")

    def test_fact_fallback_is_truncated_for_long_name_and_address(self):
        # Without a blurb, the fact-based fallback used to concatenate the
        # name, structured facts, address, AND a marketing sentence — a
        # long-named listing on a long-addressed street would blow past the
        # ~155-char snippet window Google displays. The cap now applies to
        # this path too, so search snippets aren't cut mid-word.
        desc, cap = self._describe(
            name="Beautifully Renovated Modern Rambler-Style House",
            bedrooms="4",
            bathrooms="3",
            rent="2500",
            address="12345 Northeast Broadway Street, Metropolitan City, Some State",
        )
        self.assertLessEqual(len(desc), cap)
        self.assertTrue(desc.endswith("…"))

    def test_fact_fallback_trims_trailing_rent_zero(self):
        # Rent comes back from upstream as a float; the fallback should read
        # "$1500/mo" not "$1500.0/mo".
        desc, _ = self._describe(
            name="Maple House",
            bedrooms="2",
            bathrooms="1",
            rent=1500.0,
            address="123 Main St",
        )
        self.assertIn("$1500/mo", desc)
        self.assertNotIn("$1500.0/mo", desc)

    def test_fact_fallback_uses_default_name(self):
        desc, _ = self._describe(bedrooms="1", bathrooms="1")
        self.assertTrue(desc.startswith("Rental home"))

    def test_missing_fields_are_omitted(self):
        # "N/A" placeholders should not appear as literal text in the snippet.
        desc, _ = self._describe(
            name="Maple House",
            bedrooms="N/A",
            bathrooms="N/A",
            rent="N/A",
            address="N/A",
        )
        self.assertNotIn("N/A", desc)
        self.assertNotIn(" in .", desc)


if __name__ == "__main__":
    unittest.main()
