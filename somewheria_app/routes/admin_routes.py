import csv
import datetime
import io
import os
import re
import time
import uuid as uuid_lib

import requests

from flask import Response, abort, current_app, jsonify, redirect, render_template, request, send_file, session, url_for

from ..services.auth import (
    admin_required,
    get_current_user,
    high_admin_required,
    is_logged_in,
    renter_required,
    role_rank,
)
from ..services.properties import BLANK_PROPERTY, UploadValidationError
from ..services.registry import get_services
from ..services.security import rate_limit
from ..services.timeutil import utcnow_iso
from ..services.validation import is_valid_email


ALLOWED_ROLES = ("renter", "admin", "high_admin")

# Contract PDF upload constraints. PDFs are validated by sniffing the magic
# bytes (%PDF-) and capping size at 16 MB — same ceiling as image uploads.
MAX_CONTRACT_PDF_BYTES = 16 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


def _lazy_file_status(label: str, path) -> dict:
    """Status row for a file that is created on first write.

    The appointments log, change log, and user-roles file don't exist until
    the first booking / site change / role assignment. Their absence on a
    fresh deploy is normal, so report it as healthy ("Not created yet")
    rather than a red "Missing"; only flag a real fault when the directory
    itself isn't writable, which would actually block the first write.
    """
    if path.exists():
        return {"label": label, "detail": "Present", "ok": True}
    parent = path.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK)
    if writable:
        return {"label": label, "detail": "Not created yet", "ok": True}
    return {"label": label, "detail": "Missing (directory not writable)", "ok": False}


def _zillow_status_detail(zillow) -> str:
    snapshot = zillow.status_snapshot()
    if not snapshot["configured"]:
        return (
            "Credentials missing (ZILLOW_API_BASE_URL / ZILLOW_API_TOKEN / "
            "ZILLOW_FEED_KEY); publishes are skipped."
        )
    return (
        f"{snapshot['success_count']} successful syncs, "
        f"{snapshot['failure_count']} failures since last restart"
    )


def _current_role() -> str:
    return (get_current_user() or {}).get("role", "guest")


def _can_act_on(actor_role: str, target_role: str) -> bool:
    # Admins may only act on users whose rank is strictly lower than their own.
    return role_rank(actor_role) > role_rank(target_role)


def _can_assign(actor_role: str, new_role: str) -> bool:
    # High admins may assign any role, INCLUDING high_admin — with a strict
    # rank comparison here the top role was unassignable by anyone, so no
    # user could ever be promoted to high_admin through the UI (only via
    # .env). Everyone below high_admin may still only assign roles strictly
    # under their own rank (an admin can hand out renter, not admin).
    if actor_role == "high_admin":
        return True
    return role_rank(actor_role) > role_rank(new_role)


@admin_required
def add_listing():
    return render_template(
        "edit_listing.html",
        property_id="new",
        property=BLANK_PROPERTY,
        user=get_current_user(),
    )


@admin_required
def edit_listing(property_id):
    services = get_services()
    property_data = services.properties.get_property(property_id)
    if not property_data:
        return "Property not found", 404
    return render_template(
        "edit_listing.html",
        property_id=property_id,
        property=property_data,
        user=get_current_user(),
    )


@admin_required
def save_edit(id):
    services = get_services()
    actor_email = (get_current_user() or {}).get("email", "anonymous") if is_logged_in() else "anonymous"
    try:
        if id == "new":
            services.properties.create_property(request.form, actor_email)
            return redirect(url_for("manage_listings"))
        services.properties.update_property(id, request.form, actor_email)
        return redirect(url_for("manage_listings"))
    except KeyError:
        return "Property not found", 404
    # Distinguish "couldn't reach the property service" from "the service
    # rejected the change" — collapsing every failure into one opaque 500
    # made add-listing outages (DNS, bad API key, upstream 4xx) untriageable.
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = (exc.response.text or "")[:500] if exc.response is not None else ""
        services.notifications.log_and_notify_error(
            "Save Edit Error",
            f"Upstream rejected save for {id}: HTTP {status} {body}",
        )
        return (
            f"The property service rejected the change (HTTP {status}). "
            "Check the listing fields and try again.",
            502,
        )
    except requests.RequestException as exc:
        services.notifications.log_and_notify_error(
            "Save Edit Error",
            f"Could not reach the property service saving {id}: {exc}",
        )
        return (
            "Could not reach the property service (network/DNS). "
            "The listing was NOT saved — try again once connectivity is restored.",
            503,
        )
    except Exception as exc:
        services.notifications.log_and_notify_error("Save Edit Error", f"Error saving edits for {id}: {exc}")
        return "Failed to save changes. Please try again.", 500


@admin_required
def upload_image(uuid):
    services = get_services()
    if "file" not in request.files:
        message = "No file part"
        services.notifications.log_and_notify_error("Upload Error", message)
        return jsonify(success=False, message=message), 400
    uploaded_file = request.files["file"]
    if uploaded_file.filename == "":
        message = "No selected file"
        services.notifications.log_and_notify_error("Upload Error", message)
        return jsonify(success=False, message=message), 400
    actor_email = (get_current_user() or {}).get("email", "anonymous")
    try:
        relative_url = services.properties.upload_image(
            uuid, uploaded_file, request.url_root, actor_email
        )
    except UploadValidationError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except Exception as exc:
        services.notifications.log_and_notify_error(
            "Upload Error", f"Unexpected upload failure for {uuid}: {exc}"
        )
        return jsonify(success=False, message="Upload failed."), 500
    return jsonify(success=True, new_image_url=relative_url)


@admin_required
def image_edit_notify():
    services = get_services()
    # Do NOT accept client-supplied URLs — they could be exfiltration targets
    # or used to amplify spam. Just notify that an edit occurred.
    try:
        services.notifications.notify_image_edit(
            ["(See admin console for details.)"]
        )
        return jsonify(message="Notification sent."), 200
    except Exception as exc:
        services.notifications.log_and_notify_error(
            "Image Edit Notification Error",
            f"Failed to notify image edit: {exc}",
        )
        return jsonify(message="Failed to send notification."), 500


def _contract_str_field(contract: dict, key: str) -> str:
    """Return a stripped string for ``contract[key]`` or ``""`` if absent /
    non-string.

    ``_classify_contract_status`` used to reach for ``.strip()`` / ``.lower()``
    directly on the raw values, and ``(value or "")`` only coerces when the
    stored value is falsy (``None``, ``""``). A hand-edited /
    externally-migrated ``renter_contracts`` row that put a non-string under
    ``status`` / ``start_date`` / ``end_date`` — an integer date like
    ``20241231``, or a JSON ``true`` under ``status`` — would sail past that
    guard, then raise ``AttributeError: 'int' object has no attribute 'strip'``
    inside the classifier and take out ``renter_dashboard`` /
    ``contract_detail`` via the crash handler's empty 503.
    """
    value = contract.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _classify_contract_status(contract: dict) -> str:
    """Normalize a contract's status into one of: active / pending / ended.

    Falls back to the explicit ``status`` field, then infers from dates if the
    field is unset or unrecognized. The classification is used purely for the
    dashboard summary — admins can still set any free-form status string.
    """
    raw = _contract_str_field(contract, "status").lower()
    if raw in {"active", "current"}:
        return "active"
    if raw in {"pending", "upcoming", "draft"}:
        return "pending"
    if raw in {"ended", "expired", "terminated", "closed"}:
        return "ended"
    # Infer from end_date if status is unrecognized.
    end_date = _contract_str_field(contract, "end_date")
    if end_date:
        try:
            end = datetime.date.fromisoformat(end_date[:10])
            if end < datetime.date.today():
                return "ended"
        except ValueError:
            pass
    start_date = _contract_str_field(contract, "start_date")
    if start_date:
        try:
            start = datetime.date.fromisoformat(start_date[:10])
            if start > datetime.date.today():
                return "pending"
        except ValueError:
            pass
    return "active"


def _backfill_contract_ids(services, contracts_for_email: list[dict], email: str) -> list[dict]:
    """Old contracts written before PDFs were a thing have no ``id``. Stamp
    one in (and persist the back-fill) so renter links can target them."""
    if not contracts_for_email:
        return contracts_for_email
    needs_save = any(not contract.get("id") for contract in contracts_for_email)
    if not needs_save:
        for contract in contracts_for_email:
            contract.setdefault("pdf_filename", "")
            # Annotate a normalized status for templates without mutating the
            # canonical free-form ``status`` field admins set in the form.
            contract["status_class"] = _classify_contract_status(contract)
        return contracts_for_email
    # Hold the storage lock across load+modify+save so a concurrent
    # admin add/delete on the same renter can't race the backfill and
    # silently lose one side's write. Re-read the fresh list inside the
    # lock (the caller's snapshot pre-dates any concurrent modification);
    # writing back the stale snapshot would clobber those changes. Mirrors
    # the lost-update fix in commit e062313 for tickets and pending
    # registrations.
    try:
        with services.storage.atomic():
            all_contracts = services.storage.get_renter_contracts()
            contracts_for_email = all_contracts.get(email, [])
            fresh_changed = False
            for contract in contracts_for_email:
                if not contract.get("id"):
                    contract["id"] = uuid_lib.uuid4().hex
                    fresh_changed = True
            if fresh_changed:
                all_contracts[email] = contracts_for_email
                services.storage.save_renter_contracts(all_contracts)
    except Exception:
        pass
    for contract in contracts_for_email:
        contract.setdefault("pdf_filename", "")
        contract["status_class"] = _classify_contract_status(contract)
    return contracts_for_email


@renter_required
def renter_dashboard():
    services = get_services()
    user = get_current_user()
    email = user["email"].lower()
    contracts = _backfill_contract_ids(
        services, services.storage.get_renter_contracts().get(email, []), email
    )
    my_tickets = services.tickets.list_tickets(submitter=email)
    open_ticket_count = sum(
        1 for t in my_tickets if t.get("status") in {"open", "in_progress", "awaiting_parts"}
    )
    recent_tickets = my_tickets[:3]
    return render_template(
        "renter_dashboard.html",
        user=user,
        contracts=contracts,
        recent_tickets=recent_tickets,
        open_ticket_count=open_ticket_count,
        total_ticket_count=len(my_tickets),
        title="Renter Dashboard",
    )


@high_admin_required
def analytics_dashboard():
    services = get_services()
    metrics, chart_data = services.analytics.dashboard_data(services.properties.property_count())
    return render_template(
        "analytics_dashboard.html",
        user=get_current_user(),
        metrics=metrics,
        chart_data=chart_data,
        title="Site Analytics",
    )


# Admin-level, matching the other operational tools (users, registrations,
# contracts, tickets). Only Dashboard and Analytics remain high_admin-only;
# gating Status at high_admin left plain admins with a nav link that 403'd.
@admin_required
def admin_status():
    services = get_services()
    config = services.config
    property_count = services.properties.property_count()
    pending_registrations = services.storage.get_pending_registrations()
    user_roles = services.storage.get_user_roles()
    registered_routes = set(current_app.view_functions.keys())

    def route_ready(*endpoints):
        return all(endpoint in registered_routes for endpoint in endpoints)

    # Exclude "revoked" tombstones from the count: they're deleted users
    # kept only so an .env-based role can't silently restore access on the
    # next login. Counting them here would show a growing "known users"
    # total that includes people with no access.
    active_user_count = sum(1 for role in user_roles.values() if role != "revoked")
    metrics = {
        "properties_cached": property_count,
        "pending_registrations": len(pending_registrations),
        "known_users": active_user_count,
        "cache_refresh_interval": f"{config.cache_refresh_interval}s",
    }

    cache_health = services.properties.get_cache_health()

    def _format_age(seconds):
        if seconds is None:
            return "never refreshed yet"
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s ago"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"

    upstream_status = [
        {
            "label": "Last Refresh Result",
            "detail": (
                "Not yet attempted in this process"
                if cache_health["last_attempt_ok"] is None
                else "Succeeded against the property API"
                if cache_health["last_attempt_ok"]
                else f"Failed: {cache_health['last_error']}"
            ),
            "ok": cache_health["last_attempt_ok"] is not False,
        },
        {
            "label": "Cache Age",
            "detail": f"Listings last refreshed {_format_age(cache_health['age_seconds'])}",
            "ok": cache_health["age_seconds"] is not None,
        },
        {
            "label": "Last Refresh Duration",
            "detail": (
                f"{cache_health['last_refresh_seconds']:.1f}s upstream fan-out"
                if cache_health["last_refresh_seconds"] is not None
                else "Not measured yet"
            ),
            # API Gateway caps each call at 29s; flag an aggregate fan-out
            # that is creeping toward that ceiling.
            "ok": (
                cache_health["last_refresh_seconds"] is None
                or cache_health["last_refresh_seconds"] < 25
            ),
        },
    ]

    service_status = [
        {
            "label": "Property API Base",
            "detail": "Configured" if config.api_base_url else "Missing",
            "ok": bool(config.api_base_url),
        },
        {
            "label": "Google OAuth",
            "detail": "Configured" if config.google_client_id and config.google_client_secret else "Client credentials missing",
            "ok": bool(config.google_client_id and config.google_client_secret),
        },
        {
            "label": "Email Notifications",
            "detail": "Ready" if services.notifications._email_password() else "EMAIL_APP_PASSWORD is not configured",
            "ok": bool(services.notifications._email_password()),
        },
        {
            "label": "Zillow Sync",
            "detail": _zillow_status_detail(services.zillow),
            "ok": services.zillow.credentials_configured(),
        },
        {
            "label": "Background Refresh",
            "detail": "Enabled" if not config.disable_background_threads else "Disabled for this process",
            "ok": not config.disable_background_threads,
        },
    ]

    # Do not disclose absolute paths to the UI; report only presence/absence.
    # The application log is written at startup, so its absence IS an anomaly;
    # the other three are created lazily on first write, so "not created yet"
    # is a healthy state rather than a fault.
    file_status = [
        {
            "label": "Application Log",
            "detail": "Present" if config.log_file.exists() else "Missing",
            "ok": config.log_file.exists(),
        },
        _lazy_file_status("Change Log", config.change_log_file),
        _lazy_file_status("Appointments File", config.property_appointments_file),
        _lazy_file_status("User Roles File", config.user_roles_file),
    ]

    website_status = [
        {
            "label": "Public Pages",
            "detail": "Home, For Rent, About, and Contact routes are registered",
            "ok": route_ready("home", "for_rent", "about", "contact"),
        },
        {
            "label": "Authentication",
            "detail": "Login route is active"
            if route_ready("login")
            else "Login route is missing",
            "ok": route_ready("login"),
        },
        {
            "label": "Google Sign-In",
            "detail": "OAuth credentials are configured"
            if config.google_client_id and config.google_client_secret
            else "OAuth credentials are missing",
            "ok": route_ready("google_login", "google_callback")
            and bool(config.google_client_id and config.google_client_secret),
        },
        {
            "label": "Property Listings",
            "detail": f"For Rent pages are online with {property_count} cached properties",
            "ok": route_ready("for_rent", "for_rent_json", "property_details"),
        },
        {
            "label": "Appointment Requests",
            "detail": "Scheduling endpoint is available"
            if route_ready("schedule_appointment")
            else "Scheduling endpoint is missing",
            "ok": route_ready("schedule_appointment"),
        },
        {
            "label": "Admin Tools",
            "detail": "Status, users, contracts, registrations, and tickets pages are registered",
            "ok": route_ready(
                "admin_status",
                "admin_users",
                "admin_contracts",
                "admin_registrations",
                "admin_ticket_list",
            ),
        },
        {
            "label": "PWA Support",
            "detail": "Manifest and service worker files are available",
            "ok": route_ready("manifest", "manifest_json", "service_worker")
            and config.static_dir.joinpath("manifest.webmanifest").exists()
            and config.base_dir.joinpath("service-worker.js").exists(),
        },
    ]

    return render_template(
        "admin_status.html",
        title="System Status",
        metrics=metrics,
        service_status=service_status,
        file_status=file_status,
        website_status=website_status,
        upstream_status=upstream_status,
        user=get_current_user(),
    )


@high_admin_required
def admin_dashboard_combined():
    services = get_services()
    error = None
    success = None
    actor_email = (get_current_user() or {}).get("email", "")
    actor_role = _current_role()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        action = request.form.get("action", "").strip()[:32]
        if not email:
            error = "No email provided."
        elif email == actor_email.lower():
            error = "You cannot modify your own account here."
        elif action == "delete":
            target_role = services.auth.get_user_role(email)
            if not _can_act_on(actor_role, target_role):
                error = "You cannot modify a user at or above your own role."
            elif services.storage.delete_user_role(email):
                success = (
                    f"Deactivated {email} — their access is revoked, but their "
                    "contracts, profile, and tickets are kept. Re-add the email "
                    "to restore access."
                )
                services.notifications.log_site_change(actor_email, "user_deleted", {"email": email})
            else:
                error = "User not found."
        elif action == "update":
            new_role = request.form.get("role", "").strip()
            target_role = services.auth.get_user_role(email)
            # Reject malformed emails at the admin boundary too, mirroring
            # the ``add`` action below and ``admin_users``. Without this, a
            # hand-crafted POST (or paste-error) can write a role for a
            # garbage key like ``"foo"`` that no real OAuth login can ever
            # match — it just pollutes user_roles storage and shows up in
            # the roster with no way to reach it.
            if not is_valid_email(email):
                error = "A valid email is required."
            elif new_role not in ALLOWED_ROLES:
                error = "Invalid role."
            elif not _can_act_on(actor_role, target_role) or not _can_assign(actor_role, new_role):
                error = "You cannot assign a role at or above your own."
            else:
                services.storage.set_user_role(email, new_role)
                success = f"Role for {email} updated to {new_role}."
                services.notifications.log_site_change(
                    actor_email,
                    "user_role_updated",
                    {"email": email, "role": new_role},
                )
        elif action == "add":
            new_role = request.form.get("role", "renter").strip()
            user_roles = services.storage.get_user_roles()
            # Reject malformed emails at the admin boundary too. The public
            # /register path already validates with is_valid_email; without
            # the same gate here, an admin paste-error puts a garbage entry
            # like "foo" into user_roles.json that can never match a real
            # OAuth login and just pollutes the table.
            if not is_valid_email(email):
                error = "A valid email is required."
            elif email in user_roles and user_roles.get(email) != "revoked":
                error = "User already exists."
            elif new_role not in ALLOWED_ROLES:
                error = "Invalid role."
            elif not _can_assign(actor_role, new_role):
                error = "You cannot assign a role at or above your own."
            else:
                services.storage.set_user_role(email, new_role)
                success = f"User {email} added as {new_role}."
                services.notifications.log_site_change(
                    actor_email,
                    "user_added",
                    {"email": email, "role": new_role},
                )
    metrics, chart_data = services.analytics.dashboard_data(services.properties.property_count())
    ticket_summary = services.tickets.summary()
    ticket_status_counts = services.tickets.status_counts()
    listing_activity = services.analytics.recent_listing_activity(months=12)
    # Skip "revoked" tombstones: they represent deleted users whose access
    # is gone. Left in the list, the template's role tally counts them as
    # renters (its else branch is renter) and inflates the total user count.
    active_user_roles = [
        (email, role)
        for email, role in services.storage.get_user_roles().items()
        if role != "revoked"
    ]
    return render_template(
        "admin_dashboard.html",
        user=get_current_user(),
        metrics=metrics,
        chart_data=chart_data,
        users=active_user_roles,
        ticket_summary=ticket_summary,
        ticket_status_counts=ticket_status_counts,
        listing_activity=listing_activity,
        error=error,
        success=success,
        title="Admin Dashboard",
    )


# Registration spam heuristics. Real applicants don't paste links, punycode
# domains, or the comment-spam "hs=" tracking marker into a rental
# application; bots also fill the honeypot field and submit faster than a
# person can read the form.
_REGISTRATION_SPAM = re.compile(r"https?://|www\.|://|xn--|\bhs=", re.IGNORECASE)
_MIN_REGISTER_SECONDS = 3


@rate_limit(limit=3, window_seconds=600)
def register():
    services = get_services()
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        email = request.form.get("email", "").strip().lower()[:254]
        reason = request.form.get("reason", "").strip()[:2000]
        if not name or not is_valid_email(email):
            return render_template("register.html", error="Name and a valid email are required.")
        # The timestamp is set when the form is rendered; a POST with no
        # prior GET (or one arriving inhumanly fast) is a bot. Skipped under
        # TESTING so the route tests can POST directly.
        rendered_at = session.get("register_form_ts")
        too_fast = (
            rendered_at is None or time.time() - rendered_at < _MIN_REGISTER_SECONDS
        ) and not current_app.config.get("TESTING")
        if request.form.get("website") or too_fast or _REGISTRATION_SPAM.search(f"{name} {reason}"):
            # Serve the normal success page without storing anything so the
            # bot can't tell which check it tripped.
            return render_template("register.html", success=True)
        session.pop("register_form_ts", None)
        # Someone who already holds a role doesn't need to register again.
        # Without this check an approved user could re-file (approval removes
        # them from the pending list, so the pending-list dedupe below no
        # longer sees them), producing a redundant pending row, another admin
        # email, and a path to accidentally re-approving them. Serve the same
        # success page so the form doesn't leak who is already a member.
        # Revoked users resolve to "guest" and may re-request; an admin
        # decides whether to re-approve.
        if services.auth.get_user_role(email) != "guest":
            return render_template("register.html", success=True)
        # Storage de-duplicates by email and reports whether a new row was
        # stored, so we only notify on a genuinely new request — a duplicate
        # (even one racing past a separate pre-check) can't trigger a second
        # admin email.
        newly_added = services.storage.add_pending_registration(
            {"name": name, "email": email, "reason": reason}
        )
        if newly_added:
            # The submitter's response doesn't depend on delivery, and a slow
            # or unreachable Gmail relay would otherwise stall the anonymous
            # POST for up to ``SMTP_TIMEOUT_SECONDS`` (30 s) per attempt —
            # already an obvious hang given the tight rate-limit budget.
            services.notifications.send_email_async(
                "New Registration Request",
                f"Name: {name}\nEmail: {email}\nReason: {reason}\nApprove at /admin/registrations",
            )
        return render_template("register.html", success=True)
    session["register_form_ts"] = time.time()
    return render_template("register.html")


@admin_required
def admin_registrations():
    services = get_services()
    pending = services.storage.get_pending_registrations()
    if request.method == "POST":
        action = request.form.get("action")
        email = request.form.get("email", "").strip().lower()
        if not email:
            return render_template(
                "admin_registrations.html",
                pending=pending,
                title="Pending Registrations",
                error="No email provided.",
            )
        if action not in ("approve", "reject"):
            return render_template(
                "admin_registrations.html",
                pending=pending,
                title="Pending Registrations",
                error="Invalid action.",
            )
        # Act only on an address that is actually awaiting a decision. The
        # form only ever submits rows rendered from ``pending``, so a miss
        # here is a hand-crafted POST or a stale tab racing another admin's
        # decision. Without the check, ``approve`` grants the renter role to
        # any address the request names — one that was never vetted, or one
        # already decided moments ago — and ``reject`` emails a rejection to
        # someone who never applied.
        actor_email = (get_current_user() or {}).get("email", "").lower()
        if not any((item.get("email") or "").lower() == email for item in pending):
            return render_template(
                "admin_registrations.html",
                pending=pending,
                title="Pending Registrations",
                error="That registration is no longer pending.",
            )
        if action == "approve":
            # Only write a file role when it's an upgrade. A file entry wins
            # over the .env lists in get_user_role, so blindly writing
            # "renter" here would silently demote an env-configured admin
            # who somehow landed in the pending list.
            if role_rank(services.auth.get_user_role(email)) < role_rank("renter"):
                services.storage.set_user_role(email, "renter")
            services.storage.remove_pending_registration(email)
            # Approving grants a role, so it belongs in the audit trail
            # alongside the role mutations made from /admin/users and
            # /admin/dashboard — otherwise the only record of how an account
            # gained access is the approval email in someone's inbox.
            services.notifications.log_site_change(
                actor_email, "registration_approved", {"email": email}
            )
            # The admin action doesn't depend on delivery status, so keep the
            # response snappy: a slow Gmail relay would otherwise stall each
            # approve/reject POST for up to ``SMTP_TIMEOUT_SECONDS`` (30 s).
            services.notifications.send_email_async(
                "Registration Approved",
                "Your registration for Somewheria has been approved. You can now log in.",
                to=email,
            )
        else:
            services.storage.remove_pending_registration(email)
            services.notifications.log_site_change(
                actor_email, "registration_rejected", {"email": email}
            )
            services.notifications.send_email_async(
                "Registration Rejected",
                "Your registration for Somewheria was not approved at this time.",
                to=email,
            )
        pending = services.storage.get_pending_registrations()
    return render_template("admin_registrations.html", pending=pending, title="Pending Registrations")


@admin_required
def admin_users():
    services = get_services()
    error = None
    success = None
    users = services.auth.all_user_roles()
    actor_email = (get_current_user() or {}).get("email", "").lower()
    actor_role = _current_role()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        new_role = request.form.get("role", "").strip()
        action = request.form.get("action", "").strip()[:32]
        target_role = services.auth.get_user_role(email) if email else "guest"
        if not email:
            error = "No email provided."
        elif email == actor_email:
            error = "You cannot modify your own account here."
        elif action == "delete":
            if not _can_act_on(actor_role, target_role):
                error = "You cannot modify a user at or above your own role."
            elif services.storage.delete_user_role(email):
                success = (
                    f"Deactivated {email} — their access is revoked, but their "
                    "contracts, profile, and tickets are kept. Re-add the email "
                    "to restore access."
                )
                # Mirror the audit trail emitted by the user-management form on
                # /admin/dashboard so role mutations made through THIS page
                # don't silently disappear from site_changes.log — without it
                # an admin who only uses /admin/users leaves no record of the
                # delete for compliance / incident review.
                services.notifications.log_site_change(
                    actor_email, "user_deleted", {"email": email}
                )
            else:
                error = "User not found."
        elif new_role in ALLOWED_ROLES:
            # Defense in depth: reject malformed emails before they touch
            # user_roles storage. Delete is intentionally exempt so an admin
            # can still clean up legacy entries that predate this check.
            if not is_valid_email(email):
                error = "A valid email is required."
            elif not _can_act_on(actor_role, target_role) or not _can_assign(actor_role, new_role):
                error = "You cannot assign a role at or above your own."
            else:
                services.storage.set_user_role(email, new_role)
                success = f"Role for {email} updated to {new_role}."
                services.notifications.log_site_change(
                    actor_email,
                    "user_role_updated",
                    {"email": email, "role": new_role},
                )
        else:
            error = "Invalid role."
        users = services.auth.all_user_roles()
    return render_template("admin_users.html", users=users, error=error, success=success, title="User Management")


@renter_required
def renter_profile():
    services = get_services()
    user = get_current_user()
    email = user["email"].lower()
    success = None
    if request.method == "POST":
        # Hold the storage lock across load+modify+save so two renters
        # editing their profiles concurrently can't race each other's
        # saves (the read-modify-write reads ALL profiles into memory and
        # writes them all back, so the second save would otherwise clobber
        # the first). Mirrors the lost-update fix in commit e062313 for
        # tickets and pending registrations.
        with services.storage.atomic():
            profiles = services.storage.get_renter_profiles()
            profile = profiles.get(email) or {
                "name": user.get("name", ""),
                "contact": "",
                "email_status_updates": True,
                "rcs_status_updates": True,
            }
            profile["name"] = request.form.get("name", "").strip()[:120]
            profile["contact"] = request.form.get("contact", "").strip()[:200]
            # Notification preferences are renter-only. Admins can reach this
            # page (renter_required admits them), but the ticket-update
            # subscription is for tenants — ignore the fields for any other
            # role even on a hand-crafted POST, preserving whatever is stored.
            if _current_role() == "renter":
                profile["email_status_updates"] = bool(request.form.get("email_status_updates"))
                profile["rcs_status_updates"] = bool(request.form.get("rcs_status_updates"))
            profiles[email] = profile
            services.storage.save_renter_profiles(profiles)
        success = "Profile updated."
    else:
        profiles = services.storage.get_renter_profiles()
        profile = profiles.get(email) or {
            "name": user.get("name", ""),
            "contact": "",
            "email_status_updates": True,
            "rcs_status_updates": True,
        }
        # Backfill fields for profiles created before the preference existed.
        profile.setdefault("email_status_updates", True)
        profile.setdefault("rcs_status_updates", True)
    return render_template("renter_profile.html", profile=profile, user=user, success=success, title="Edit Profile")


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_iso_date(value: str) -> bool:
    """Return True if ``value`` is a strict YYYY-MM-DD calendar date.

    ``date.fromisoformat`` on 3.11+ accepts other ISO 8601 shapes — the
    week date ``2020-W12-1`` and the basic form with trailing garbage
    ``"20201212XX"`` both parse cleanly, and both happen to be 10 chars,
    so a length check alone lets them through. The regex enforces the
    exact ``YYYY-MM-DD`` extended-form shape admin contracts store; the
    ``fromisoformat`` call then confirms the month/day are a real date.
    Without both checks a hand-crafted POST could smuggle a non-canonical
    date string into ``renter_contracts`` where templates render it as
    the contract's term.
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_contract_pdf(uploaded_file) -> bytes | None:
    """Read the upload, validate as a PDF (magic bytes + size). Returns bytes
    on success or None when no file was supplied. Raises ValueError on bad
    content."""
    if not uploaded_file or not getattr(uploaded_file, "filename", ""):
        return None
    raw = uploaded_file.stream.read(MAX_CONTRACT_PDF_BYTES + 1)
    if not raw:
        return None
    if len(raw) > MAX_CONTRACT_PDF_BYTES:
        raise ValueError("PDF exceeds maximum allowed size (16 MB).")
    if not raw.startswith(PDF_MAGIC):
        raise ValueError("Uploaded file is not a valid PDF.")
    # Be conservative: also ensure the declared filename ends with .pdf.
    if not uploaded_file.filename.lower().endswith(".pdf"):
        raise ValueError("Uploaded file must have a .pdf extension.")
    return raw


def _safe_contract_pdf_path(services, pdf_filename: str):
    """Resolve a stored contract PDF filename to an absolute path inside the
    upload directory, or return None if the name is missing or escapes the
    upload root. Filenames are generated server-side as ``<uuid>.pdf`` and
    should never contain separators, but ``renter_contracts.json`` can be
    hand-edited, so this re-validates before any filesystem op (download or
    delete) to prevent path traversal."""
    if not pdf_filename:
        return None
    if "/" in pdf_filename or "\\" in pdf_filename or ".." in pdf_filename:
        return None
    upload_root = services.config.contract_upload_dir.resolve()
    pdf_path = (services.config.contract_upload_dir / pdf_filename).resolve()
    try:
        pdf_path.relative_to(upload_root)
    except ValueError:
        return None
    return pdf_path


@admin_required
def admin_contracts():
    services = get_services()
    contracts_data = services.storage.get_renter_contracts()
    error = None
    success = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            renter_email = request.form.get("renter_email", "").strip().lower()[:254]
            property_name = request.form.get("property_name", "").strip()[:200]
            start_date = request.form.get("start_date", "").strip()[:32]
            end_date = request.form.get("end_date", "").strip()[:32]
            status = request.form.get("status", "Active").strip()[:32]
            if not all([renter_email, property_name, start_date, end_date]):
                error = "All fields are required."
            elif not is_valid_email(renter_email):
                # Reject malformed renter emails before they touch
                # renter_contracts storage. A typo like "renter@" otherwise
                # creates an orphan contract that no real OAuth login can
                # ever surface for the renter.
                error = "A valid renter email is required."
            elif not _valid_iso_date(start_date) or not _valid_iso_date(end_date):
                # Reject garbage date strings at the boundary. The <input
                # type="date"> field normalizes to YYYY-MM-DD, so a bad value
                # here is a hand-crafted POST or a paste error — either way
                # ``_classify_contract_status`` would silently swallow the
                # parse failure downstream and the admin dashboard would
                # render the literal junk string as the contract's term.
                error = "Start and end dates must be YYYY-MM-DD."
            elif datetime.date.fromisoformat(end_date) < datetime.date.fromisoformat(start_date):
                # A contract whose end precedes its start is nonsense and
                # would classify as "ended" the moment it's saved. Rejecting
                # here catches the most likely typo (year swap, month swap)
                # while the admin still has the form open to correct it.
                error = "End date must not be before start date."
            else:
                contract_id = uuid_lib.uuid4().hex
                pdf_filename = ""
                try:
                    pdf_bytes = _validate_contract_pdf(request.files.get("contract_pdf"))
                except ValueError as exc:
                    error = str(exc)
                    pdf_bytes = None
                if error is None:
                    if pdf_bytes:
                        pdf_filename = f"{contract_id}.pdf"
                        target = services.config.contract_upload_dir / pdf_filename
                        try:
                            ok = services.storage.save_binary_file(target, pdf_bytes)
                        except Exception:
                            ok = False
                        if not ok:
                            pdf_filename = ""
                            error = "Failed to save PDF; contract not added."
                if error is None:
                    new_contract = {
                        "id": contract_id,
                        "property_name": property_name,
                        "start_date": start_date,
                        "end_date": end_date,
                        "status": status,
                        "pdf_filename": pdf_filename,
                        "created_at": utcnow_iso(),
                    }
                    # Hold the storage lock across load+modify+save so two
                    # admins adding contracts concurrently can't both read
                    # the pre-add snapshot and then race each other's saves
                    # (which silently drops one of the new contracts on the
                    # file backend). Same class of fix as commit e062313
                    # made for tickets and pending registrations.
                    with services.storage.atomic():
                        contracts_data = services.storage.get_renter_contracts()
                        contracts_data.setdefault(renter_email, []).append(new_contract)
                        services.storage.save_renter_contracts(contracts_data)
                    success = f"Contract added for {renter_email}."
        elif action == "delete":
            renter_email = request.form.get("renter_email", "").strip().lower()
            contract_index = request.form.get("contract_index")
            if renter_email and contract_index is not None:
                try:
                    contract_idx = int(contract_index)
                    # Hold the storage lock across load+modify+save so a
                    # delete racing another admin's add (or delete) on the
                    # same renter can't drop one side's write. The PDF
                    # unlink and the JSON save run together so the file and
                    # the metadata stay in sync.
                    with services.storage.atomic():
                        contracts_data = services.storage.get_renter_contracts()
                        if renter_email in contracts_data and 0 <= contract_idx < len(contracts_data[renter_email]):
                            removed = contracts_data[renter_email][contract_idx]
                            # Best-effort delete of the on-disk PDF. Validate
                            # the filename the same way the download path
                            # does so a tampered ``pdf_filename`` can't
                            # unlink an arbitrary file outside the upload
                            # directory.
                            pdf_filename = removed.get("pdf_filename") or ""
                            safe_pdf_path = _safe_contract_pdf_path(services, pdf_filename)
                            if safe_pdf_path is not None:
                                try:
                                    services.storage.delete_file(safe_pdf_path)
                                except Exception:
                                    pass
                            del contracts_data[renter_email][contract_idx]
                            if not contracts_data[renter_email]:
                                del contracts_data[renter_email]
                            services.storage.save_renter_contracts(contracts_data)
                            success = f"Contract removed for {renter_email}."
                        else:
                            error = "Contract not found."
                except ValueError:
                    error = "Invalid contract index."
            else:
                error = "Missing required fields."
    return render_template(
        "admin_contracts.html",
        contracts=contracts_data,
        error=error,
        success=success,
        title="Contract Management",
    )


def _find_contract_for_email(services, email: str, contract_id: str):
    """Return (contract_dict, list_index) for a given (email, contract_id),
    or (None, None) if not found."""
    contracts = services.storage.get_renter_contracts().get(email, [])
    for idx, contract in enumerate(contracts):
        if contract.get("id") == contract_id:
            return contract, idx
    return None, None


@renter_required
def contract_detail(contract_id: str):
    services = get_services()
    user = get_current_user()
    email = user["email"].lower()
    is_admin = user.get("role") in ("admin", "high_admin")
    contract = None
    owner_email = email
    if is_admin:
        # Admins can view any contract — search across renters.
        for renter_email, items in services.storage.get_renter_contracts().items():
            for item in items:
                if item.get("id") == contract_id:
                    contract = item
                    owner_email = renter_email
                    break
            if contract:
                break
    else:
        # Make sure the renter's own contracts have IDs (backfill for old data).
        _backfill_contract_ids(
            services, services.storage.get_renter_contracts().get(email, []), email
        )
        contract, _ = _find_contract_for_email(services, email, contract_id)
    if not contract:
        return render_template("404.html", title="Contract Not Found"), 404
    # Always recompute — the JSON on disk may carry a stale ``status_class``
    # persisted by an older backfill, and ``setdefault`` would keep the
    # stale value instead of refreshing it against today's date.
    contract["status_class"] = _classify_contract_status(contract)
    return render_template(
        "contract_detail.html",
        contract=contract,
        owner_email=owner_email,
        is_admin=is_admin,
        title=f"Contract: {contract.get('property_name', '')}",
        user=user,
    )


@renter_required
def contract_download(contract_id: str):
    services = get_services()
    user = get_current_user()
    email = user["email"].lower()
    is_admin = user.get("role") in ("admin", "high_admin")
    contract = None
    if is_admin:
        for items in services.storage.get_renter_contracts().values():
            for item in items:
                if item.get("id") == contract_id:
                    contract = item
                    break
            if contract:
                break
    else:
        contract, _ = _find_contract_for_email(services, email, contract_id)
    if not contract:
        abort(404)
    pdf_filename = contract.get("pdf_filename") or ""
    # Defense-in-depth: re-validate the stored filename against path traversal
    # before touching the filesystem (filenames are server-generated UUIDs but
    # storage can be hand-edited).
    pdf_path = _safe_contract_pdf_path(services, pdf_filename)
    if pdf_path is None or not pdf_path.exists():
        abort(404)
    try:
        return send_file(
            str(pdf_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"contract-{contract_id[:8]}.pdf",
        )
    except Exception:
        abort(404)


@admin_required
def delete_listing(id):
    services = get_services()
    actor_email = (get_current_user() or {}).get("email", "anonymous") if is_logged_in() else "anonymous"
    try:
        services.properties.delete_property(id, actor_email)
        return redirect(url_for("manage_listings"))
    except KeyError:
        return "Property not found", 404
    except Exception as exc:
        services.notifications.log_and_notify_error(
            "Property Delete Error",
            f"Failed to delete property {id} via API: {exc}",
        )
        return "Operation failed. Please try again.", 500


@admin_required
def toggle_sale(id):
    services = get_services()
    actor_email = (get_current_user() or {}).get("email", "anonymous") if is_logged_in() else "anonymous"
    try:
        services.properties.toggle_sale(id, actor_email)
        return redirect(url_for("manage_listings"))
    except KeyError:
        return "Property not found", 404
    except Exception as exc:
        services.notifications.log_and_notify_error(
            "Toggle Sale Error",
            f"Failed to toggle for_sale for {id}: {exc}",
        )
        return "Operation failed. Please try again.", 500


@admin_required
def toggle_active(id):
    # Reversible unpublish — the counterpart to the permanent delete. Hides
    # the listing from the public site (or shows it again) while keeping the
    # upstream record intact.
    services = get_services()
    actor_email = (get_current_user() or {}).get("email", "anonymous") if is_logged_in() else "anonymous"
    try:
        make_active = services.properties.is_listing_hidden(id)
        services.properties.set_listing_active(id, active=make_active, actor_email=actor_email)
        return redirect(url_for("manage_listings"))
    except KeyError:
        return "Property not found", 404
    except Exception as exc:
        services.notifications.log_and_notify_error(
            "Toggle Active Error",
            f"Failed to toggle active for {id}: {exc}",
        )
        return "Operation failed. Please try again.", 500


def _csv_filename(prefix: str) -> str:
    today = datetime.date.today().isoformat()
    return f"{prefix}-{today}.csv"


# Leading characters that Excel / LibreOffice / Google Sheets interpret as the
# start of a formula. A renter who submits a ticket titled ``=cmd|'/c calc'!A1``
# would otherwise execute that formula when an admin opens the exported file.
# Defuse by prefixing the cell with a single quote — same mitigation OWASP
# recommends. Tab and CR are included because some spreadsheet apps treat them
# as cell-leading triggers via auto-detection.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Return ``value`` as a CSV cell that cannot be interpreted as a formula.

    Non-string values are coerced via ``str`` so admins still see the raw data
    (e.g. integers from JIRA metadata). Empty strings pass through unchanged.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text and text[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + text
    return text


@admin_required
def admin_contracts_export_csv():
    services = get_services()
    contracts_by_renter = services.storage.get_renter_contracts() or {}

    columns = (
        "renter_email",
        "property_name",
        "start_date",
        "end_date",
        "status",
        "created_at",
    )

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for renter_email, contracts in contracts_by_renter.items():
            for contract in contracts or []:
                writer.writerow([
                    _csv_safe(renter_email),
                    _csv_safe(contract.get("property_name", "")),
                    _csv_safe(contract.get("start_date", "")),
                    _csv_safe(contract.get("end_date", "")),
                    _csv_safe(contract.get("status", "")),
                    _csv_safe(contract.get("created_at", "")),
                ])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{_csv_filename("contracts")}"',
            "Cache-Control": "no-store",
        },
    )


@admin_required
def admin_tickets_export_csv():
    services = get_services()
    tickets = services.tickets.list_tickets()

    columns = (
        "id",
        "title",
        "status",
        "priority",
        "category",
        "submitter",
        "property_name",
        "created_at",
        "last_updated",
    )

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for ticket in tickets:
            writer.writerow([
                _csv_safe(ticket.get("id", "")),
                _csv_safe(ticket.get("title", "")),
                _csv_safe(ticket.get("status", "")),
                _csv_safe(ticket.get("priority", "")),
                _csv_safe(ticket.get("category", "")),
                _csv_safe(ticket.get("submitted_by", "")),
                _csv_safe(ticket.get("property_name", "")),
                _csv_safe(ticket.get("created_at", "")),
                _csv_safe(ticket.get("updated_at", "")),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return Response(
        generate(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{_csv_filename("tickets")}"',
            "Cache-Control": "no-store",
        },
    )


def register_admin_routes(app) -> None:
    app.add_url_rule("/add-listing", endpoint="add_listing", view_func=add_listing)
    app.add_url_rule("/edit-listing/<property_id>", endpoint="edit_listing", view_func=edit_listing)
    app.add_url_rule("/save-edit/<id>", endpoint="save_edit", view_func=save_edit, methods=["POST"])
    app.add_url_rule("/upload-image/<uuid>", endpoint="upload_image", view_func=upload_image, methods=["POST"])
    app.add_url_rule(
        "/image-edit-notify",
        endpoint="image_edit_notify",
        view_func=image_edit_notify,
        methods=["POST"],
    )
    app.add_url_rule("/renter-dashboard", endpoint="renter_dashboard", view_func=renter_dashboard)
    app.add_url_rule("/admin/analytics", endpoint="analytics_dashboard", view_func=analytics_dashboard)
    app.add_url_rule("/admin/status", endpoint="admin_status", view_func=admin_status)
    app.add_url_rule(
        "/admin/dashboard",
        endpoint="admin_dashboard_combined",
        view_func=admin_dashboard_combined,
        methods=["GET", "POST"],
    )
    app.add_url_rule("/register", endpoint="register", view_func=register, methods=["GET", "POST"])
    app.add_url_rule(
        "/admin/registrations",
        endpoint="admin_registrations",
        view_func=admin_registrations,
        methods=["GET", "POST"],
    )
    app.add_url_rule("/admin/users", endpoint="admin_users", view_func=admin_users, methods=["GET", "POST"])
    app.add_url_rule(
        "/renter/profile",
        endpoint="renter_profile",
        view_func=renter_profile,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/contracts",
        endpoint="admin_contracts",
        view_func=admin_contracts,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/contracts/export.csv",
        endpoint="admin_contracts_export_csv",
        view_func=admin_contracts_export_csv,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/tickets/export.csv",
        endpoint="admin_tickets_export_csv",
        view_func=admin_tickets_export_csv,
        methods=["GET"],
    )
    app.add_url_rule(
        "/contracts/<contract_id>",
        endpoint="contract_detail",
        view_func=contract_detail,
        methods=["GET"],
    )
    app.add_url_rule(
        "/contracts/<contract_id>/download",
        endpoint="contract_download",
        view_func=contract_download,
        methods=["GET"],
    )
    app.add_url_rule(
        "/delete-listing/<id>",
        endpoint="delete_listing",
        view_func=delete_listing,
        methods=["POST"],
    )
    app.add_url_rule("/toggle-sale/<id>", endpoint="toggle_sale", view_func=toggle_sale, methods=["POST"])
    app.add_url_rule("/toggle-active/<id>", endpoint="toggle_active", view_func=toggle_active, methods=["POST"])
