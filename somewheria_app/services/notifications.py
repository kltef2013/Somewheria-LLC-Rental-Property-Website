import collections
import html
import json
import os
import re
import smtplib
import threading
import time
from email.message import EmailMessage

from .console import get_console_logger
from .timeutil import utcnow_iso
from .validation import is_valid_email


# Hard cap on the SMTP socket so a slow / unreachable Gmail relay can't hang
# the calling thread indefinitely. Crash-handler emails run in daemon
# threads — without a timeout, an outage upstream would silently accumulate
# stuck threads (one per unique crash fingerprint per 10 minutes) holding
# sockets and file descriptors. 30s is generous for a real TLS+login+send
# round trip while still bounding worst-case stalls.
SMTP_TIMEOUT_SECONDS = 30

# Suppress repeat ``log_and_notify_error`` emails for the same subject within
# this window. Matches the 10-minute cooldown ``_send_crash_email_async`` in
# ``somewheria_app/__init__.py`` already applies per (ExceptionType, path)
# fingerprint. Analytics/console logging stay per-event; only the SMTP send
# is throttled — a burst of the same failure shouldn't mail-bomb the admin
# inbox or fan out one daemon thread per event.
ERROR_EMAIL_COOLDOWN_SECONDS = 600

# Matches any run of CR/LF/NUL. ``EmailMessage`` rejects header values that
# contain CR or LF with a hard ValueError (its guard against header
# injection), so a subject built from user-supplied text — a ticket title,
# an admin's status label — that happens to contain a stray newline would
# otherwise turn every downstream send into a silent failure.
_SUBJECT_UNSAFE_CHARS = re.compile(r"[\r\n\x00]+")

# Upper bound on the outbound Subject header. RFC 5322 recommends 78 chars
# per line and hard-caps at 998; long subjects still send, but many mail
# clients truncate them awkwardly. Truncating here also keeps a hostile
# multi-megabyte string from riding the header all the way to the relay.
_SUBJECT_MAX_LEN = 200


def _sanitize_subject(subject: str) -> str:
    """Collapse header-hostile characters so ``EmailMessage`` never rejects
    the subject. Callers pass ``subject`` values that are sometimes built from
    user input (ticket titles); a CR or LF in that string turns the entire
    send into a hard ValueError inside ``EmailMessage.__setitem__``, which
    the outer try/except in :meth:`NotificationService.send_email` then
    swallows as "failed to send" — silently dropping every ticket / update
    / note notification whose title happened to contain a newline. Also
    bounds the length so a runaway subject can't blow past mail-relay
    limits."""
    text = str(subject or "")
    text = _SUBJECT_UNSAFE_CHARS.sub(" ", text).strip()
    if len(text) > _SUBJECT_MAX_LEN:
        text = text[: _SUBJECT_MAX_LEN - 1].rstrip() + "…"
    return text


class NotificationService:
    def __init__(self, config, analytics) -> None:
        self.config = config
        self.analytics = analytics
        self.console = get_console_logger("notify")
        # Serializes appends to ``change_log_file`` across concurrent request
        # threads. POSIX guarantees a single ``write()`` syscall on an
        # O_APPEND file is atomic only up to PIPE_BUF (typically 4096 bytes).
        # A ``properties_cache_updated`` entry that lists field diffs across
        # many changed properties easily exceeds that ceiling, so a
        # concurrent smaller write (a ticket_created, a user_added) can
        # interleave into the middle of it and corrupt the JSONL — which
        # ``analytics.recent_listing_activity`` then silently drops.
        self._change_log_lock = threading.Lock()
        # Fingerprint -> monotonic timestamp of the last error-notification
        # SMTP dispatch. Bounded because we prune every stale entry each
        # time we admit a new one (see ``log_and_notify_error``).
        self._error_email_lock = threading.Lock()
        self._error_email_last_sent: dict[str, float] = {}

    def send_email(self, subject: str, body: str, to: str | None = None) -> bool:
        app_password = self._email_password()
        if not app_password:
            self.console.warning("EMAIL_APP_PASSWORD is not configured; skipping email '%s'", subject)
            return False

        recipient = (to or self.config.email_recipient or "").strip()
        if not is_valid_email(recipient):
            self.console.warning("No valid recipient for email '%s' (to=%r); skipping.", subject, to)
            return False

        safe_subject = _sanitize_subject(subject)
        message = EmailMessage()
        message["Subject"] = safe_subject
        message["From"] = self.config.email_sender
        message["To"] = recipient
        message.set_content(body)
        message.add_alternative(self._html_email_body(safe_subject, body), subtype="html")

        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(self.config.email_sender, app_password)
                server.send_message(message)
            self.console.info("Sent email '%s' to %s", subject, recipient)
            return True
        except Exception as exc:
            self.console.error("Failed to send email '%s': %s", subject, exc)
            return False

    def send_email_async(self, subject: str, body: str, to: str | None = None) -> None:
        """Dispatch ``send_email`` off the request thread.

        ``send_email`` blocks up to ``SMTP_TIMEOUT_SECONDS`` (30 s) per call —
        long enough for a slow Gmail relay to stall a request handler well past
        the user's patience. Callers that don't rely on the return value
        (appointment confirmations, report-issue notices, anything filed
        fire-and-forget) should use this wrapper to keep the response snappy.

        Honours ``DISABLE_BACKGROUND_THREADS=1`` so test runs stay synchronous
        and can assert against a mocked ``send_email``. Mirrors the pattern
        already in ``TicketService._enqueue_email``; the crash-handler's own
        daemon-thread dispatch is left in place because it runs from an
        ``errorhandler`` and needs its own per-fingerprint suppression state.
        """
        if os.getenv("DISABLE_BACKGROUND_THREADS") == "1":
            try:
                self.send_email(subject, body, to=to)
            except Exception as exc:
                self.console.warning(
                    "Inline async email send failed for %r: %s", subject, exc
                )
            return

        def _worker() -> None:
            try:
                self.send_email(subject, body, to=to)
            except Exception as exc:
                self.console.warning(
                    "Background async email send failed for %r: %s", subject, exc
                )

        threading.Thread(target=_worker, name="notify-email", daemon=True).start()

    def _html_email_body(self, subject: str, body: str) -> str:
        escaped_subject = html.escape(subject)
        body_lines = [line.strip() for line in body.splitlines() if line.strip()]
        intro = html.escape(body_lines[0]) if body_lines else "There is a new update from Somewheria."
        details = "".join(
            f"<p style=\"margin:0 0 12px;font-size:14px;line-height:1.65;color:#5a4439;\">{html.escape(line)}</p>"
            for line in body_lines[1:]
        )
        if not details:
            details = (
                "<p style=\"margin:0 0 12px;font-size:14px;line-height:1.65;color:#5a4439;\">"
                "Open the dashboard or logs for the latest details."
                "</p>"
            )

        return f"""
<html>
  <body style="margin:0;padding:24px;font-family:Arial,sans-serif;background:#f7f1ea;color:#352118;">
    <div style="max-width:600px;margin:0 auto;background:#fffaf5;border-radius:24px;padding:30px;box-shadow:0 18px 36px rgba(62,42,32,0.14);border:1px solid #eedfd2;">
      <div style="display:inline-block;padding:6px 12px;border-radius:999px;background:#efe1d4;color:#7a6257;font-size:11px;letter-spacing:2px;text-transform:uppercase;">
        Somewheria LLC
      </div>
      <h2 style="margin:16px 0 8px;font-size:24px;color:#3e2a20;">{escaped_subject}</h2>
      <p style="margin:0 0 18px;font-size:14px;line-height:1.65;color:#5a4439;">{intro}</p>
      <div style="background:#f7ede2;border:1px solid #e7d7c8;border-radius:18px;padding:18px 20px;">
        {details}
      </div>
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #f1e6db;">
        <p style="margin:0;font-size:12px;color:#7a6257;">This notification was sent automatically by the Somewheria management site.</p>
        <p style="margin:8px 0 0;font-size:12px;color:#7a6257;">Ekberg Properties admin tools</p>
      </div>
    </div>
  </body>
</html>
"""

    def _email_password(self) -> str:
        import os

        return os.getenv("EMAIL_APP_PASSWORD", "")

    def log_and_notify_error(self, subject: str, error_message: str) -> None:
        """Record an error and fire an admin notification without stalling the caller.

        Analytics + console logging stay synchronous — those are in-memory
        counters and stdout writes, and a background thread could be killed by
        a shutdown before it runs. The SMTP send is dispatched through
        ``send_email_async`` so a slow / unreachable Gmail relay can't add up
        to ``SMTP_TIMEOUT_SECONDS`` (30 s) of latency to whatever request
        thread triggered the failure. All 13+ callers (admin/public/auth/
        ticket routes plus the zillow retry worker) ignore the return value,
        so nothing depended on blocking until delivery finished.

        Repeat SMTP dispatches for the same ``subject`` are suppressed for
        ``ERROR_EMAIL_COOLDOWN_SECONDS`` (10 min). Analytics + console still
        run on every event; only the outbound email is throttled. Without
        this a sustained failure (upstream outage, an OAuth callback under
        a spam scan, a repeatedly-failing property save) would mail-bomb
        the admin inbox and fan out one 30s-timeout daemon thread per
        event. Mirrors the fingerprint-cooldown ``_send_crash_email_async``
        in ``somewheria_app/__init__.py`` already applies to unhandled
        exceptions.
        """
        self.analytics.record_error()
        self.console.error("%s: %s", subject, error_message)
        if self._should_send_error_email(subject):
            self.send_email_async(subject, error_message)

    def _should_send_error_email(self, subject: str) -> bool:
        # Fingerprint by subject: subjects are static strings in every caller
        # (e.g. "Google OAuth Error", "Save Edit Error", "Zillow Sync
        # Failure") — the varying detail lives in the body. Truncated so a
        # hypothetical dynamic subject can't grow the dict key without
        # bound.
        key = (subject or "")[:200]
        now = time.monotonic()
        with self._error_email_lock:
            last = self._error_email_last_sent.get(key)
            if last is not None and now - last < ERROR_EMAIL_COOLDOWN_SECONDS:
                return False
            # Prune stale entries opportunistically: anything older than the
            # cooldown window can no longer suppress a send, so it has no
            # value in the map. Keeps the map bounded even under a
            # long-running process that trips many distinct subjects.
            cutoff = now - ERROR_EMAIL_COOLDOWN_SECONDS
            stale = [k for k, ts in self._error_email_last_sent.items() if ts < cutoff]
            for k in stale:
                del self._error_email_last_sent[k]
            self._error_email_last_sent[key] = now
            return True

    def notify_image_edit(self, image_urls: list[str]) -> None:
        # Dispatched off the request thread: callers
        # (``PropertyService.upload_image`` and the ``/image-edit-notify``
        # admin endpoint) don't observe the return value and the response
        # they render is unrelated to delivery, so blocking them for up to
        # ``SMTP_TIMEOUT_SECONDS`` (30 s) on a slow Gmail relay is pure
        # user-facing latency on top of the image-upload path (which already
        # does an S3 associate + cache refresh). Mirrors the pattern in
        # PRs #136-#138 for the other request-hot notification sites.
        self.send_email_async(
            "Image Edited Notification",
            "The following image(s) have been edited:\n" + "\n".join(image_urls),
        )

    def log_site_change(self, user_email: str, action: str, extra: dict | None = None) -> None:
        try:
            entry = {
                # UTC so analytics.recent_listing_activity buckets entries by
                # the same calendar month regardless of the server's local
                # timezone — a naive local-time isoformat() looks UTC-shaped
                # but silently drifts buckets across the midnight boundary.
                "timestamp": utcnow_iso(),
                "user": user_email or "anonymous",
                "action": action,
                "extra": extra or {},
            }
            # Serialize the JSON outside the lock (CPU work only) and hold
            # the lock across the open+write+close so a concurrent writer
            # cannot interleave its own line inside ours. Without this, a
            # large ``properties_cache_updated`` entry racing a small
            # ``ticket_created`` entry can produce a corrupt JSONL row that
            # ``analytics.recent_listing_activity`` silently drops.
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with self._change_log_lock:
                with self.config.change_log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception as exc:
            self.console.error("Failed to record site change '%s': %s", action, exc)

    def read_logs(self) -> list[dict]:
        if not self.config.log_file.exists():
            return []
        ansi_escape = re.compile(r"\x1B\[[0-9;]*[mK]")
        # Bound peak memory: a long-running process can produce a multi-MB
        # log file, but the UI only ever shows the last 500 entries. A deque
        # keeps just the tail in memory instead of the entire history.
        entries: collections.deque[dict] = collections.deque(maxlen=500)
        with self.config.log_file.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                request_id = "-"
                # Current format: one JSON object per line. Fall back to the
                # older pipe / colon text formats so entries written before the
                # structured-logging upgrade still render during the transition.
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        obj = None
                    if isinstance(obj, dict):
                        # Coerce every field to a string. In normal operation
                        # the writer only emits strings, but a hand-edited /
                        # corrupted JSON row where ``message`` is ``null`` or a
                        # number would otherwise raise ``TypeError`` inside
                        # ``ansi_escape.sub`` below (when ``component`` is
                        # absent so the intervening f-string doesn't coerce
                        # it first) and take the /logs viewer out via the
                        # crash handler's empty 503. Mirrors the isinstance
                        # guards PRs #144 / #147 / #148 already apply on the
                        # change-log JSONL, tickets, and renter profiles.
                        timestamp = obj.get("timestamp") or ""
                        if not isinstance(timestamp, str):
                            timestamp = str(timestamp)
                        level_raw = obj.get("level") or ""
                        level = level_raw if isinstance(level_raw, str) else str(level_raw)
                        message_raw = obj.get("message")
                        if message_raw is None:
                            message = ""
                        elif isinstance(message_raw, str):
                            message = message_raw
                        else:
                            message = str(message_raw)
                        component_raw = obj.get("component") or ""
                        component = component_raw if isinstance(component_raw, str) else str(component_raw)
                        request_id_raw = obj.get("request_id")
                        request_id = request_id_raw if isinstance(request_id_raw, str) and request_id_raw else "-"
                        if component:
                            message = f"[{component}] {message}"
                    else:
                        timestamp, level, message = "", "", line
                elif "|" in line:
                    pipe_parts = line.split("|", 3)
                    if len(pipe_parts) == 4:
                        timestamp, level, component, message = pipe_parts
                        message = f"[{component}] {message}"
                    else:
                        timestamp, level, message = "", "", line
                else:
                    legacy_parts = line.split(":", 2)
                    if len(legacy_parts) == 3:
                        timestamp, level, message = legacy_parts
                    else:
                        timestamp, level, message = "", "", line
                if level == "WARN":
                    level = "WARNING"
                if level == "CRIT":
                    level = "CRITICAL"
                entries.append(
                    {
                        "timestamp": timestamp or "Unknown",
                        "level": level,
                        "request_id": request_id,
                        "message": ansi_escape.sub("", message),
                    }
                )
        return list(reversed(entries))
