"""No service is contacted: all HTTP traffic uses an explicit MockTransport."""

import json
import subprocess

import httpx
import pytest
from cripto_core.errors import PaperError
from cripto_core.serialization import fingerprint

from cripto_bot.adapters.continuous_config import NotificationInput, endpoint
from cripto_bot.adapters.notifications.http import LocalSender
from cripto_bot.adapters.storage.notification_sqlite import NotificationStore
from cripto_bot.application.notifications import ProcessSender


def event(at=1000, category="STARTED"):
    body = {"pilot_id": "drill", "category": category, "at": at, "reason": "stale"}
    return {**body, "event_id": fingerprint(body)}


def settings():
    return NotificationInput(
        enabled=True,
        mail_sender_url="http://127.0.0.1:8002",
        mail_to="test@example.invalid",
        whatsapp_enabled=True,
        whatsapp_notify_url="http://127.0.0.1:8003",
        whatsapp_contact="test-only",
    )


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"message": "E-mail enviado com sucesso."}, "CONFIRMED"),
        (200, {"message": "ok"}, "UNKNOWN"),
        (202, {}, "UNKNOWN"),
        (400, {}, "FAILED"),
        (500, {}, "UNKNOWN"),
        (302, {}, "UNKNOWN"),
    ],
)
def test_mail_semantics_and_payload(status, body, expected):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/api/v1/mail/send" and request.method == "POST"
        payload = json.loads(request.content)
        assert set(payload) == {"to", "subject", "body", "message_type"}
        assert "authorization" not in request.headers and "x-mbx-apikey" not in request.headers
        return httpx.Response(status, json=body, headers={"Location": "https://blocked.invalid"})

    sender = LocalSender(settings(), transport=httpx.MockTransport(handle))
    assert sender.send(event(), "mail") == expected
    assert len(requests) == 1


@pytest.mark.parametrize("opened,expected", [(True, "CONFIRMED"), (False, "FAILED")])
def test_whatsapp_only_status_then_send(opened, expected):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "SESSAO_ABERTA" if opened else "FECHADA"})
        assert set(json.loads(request.content)) == {"contact", "message"}
        return httpx.Response(200, json={"status": "enviado"})

    assert (
        LocalSender(settings(), transport=httpx.MockTransport(handle)).send(event(), "whatsapp")
        == expected
    )
    assert paths == (
        ["/whatsapp/session/status", "/whatsapp/messages/send"]
        if opened
        else ["/whatsapp/session/status"]
    )


@pytest.mark.parametrize("channel,expected", [("mail", "UNKNOWN"), ("whatsapp", "FAILED")])
def test_timeout_uncertainty_depends_on_post(channel, expected):
    def handle(request):
        raise httpx.ReadTimeout("test", request=request)

    assert (
        LocalSender(settings(), transport=httpx.MockTransport(handle)).send(event(), channel)
        == expected
    )


def test_response_limit_and_total_process_deadline(monkeypatch, tmp_path):
    def handle(request):
        return httpx.Response(200, content=b"x" * 16385)

    assert (
        LocalSender(settings(), transport=httpx.MockTransport(handle)).send(event(), "mail")
        == "UNKNOWN"
    )

    def timeout(args, **kwargs):
        assert "-I" in args and kwargs["timeout"] == 10 and "shell" not in kwargs
        raise subprocess.TimeoutExpired(args, 10)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert ProcessSender(settings(), tmp_path).send(event(), "mail") == "UNKNOWN"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:443",
        "http://u:p@localhost:80",
        "http://localhost",
        "http://127.0.0.1:80/other",
        "http://localhost:80?x=1",
        "http://localhost:80/#x",
    ],
)
def test_nonlocal_or_ambiguous_url_rejected(url):
    with pytest.raises(PaperError):
        endpoint(url)


def test_queue_lease_unknown_fallback_confirmed_does_not_retry(tmp_path):
    with NotificationStore(tmp_path / "queue") as queue:
        e = event()
        queue.enqueue(e, fingerprint(e), whatsapp=True)
        queue.enqueue(e, fingerprint(e), whatsapp=True)
        first = queue.claim(1000)
        assert first["channel"] == "whatsapp" and first["attempt_no"] == 1
        assert queue.claim(60999) is None
        fallback = queue.claim(61000)
        assert fallback["channel"] == "mail" and fallback["duplicate_possible"]
        assert queue.summary()["unknown_attempts"] == 1
        queue.finish(fallback, "CONFIRMED", 61001)
        queue.finish(fallback, "CONFIRMED", 61001)
        assert queue.claim(99999999) is None
        assert queue.summary()["states"] == {"CONFIRMED": 1}


def test_retries_are_bounded_and_coalescing_preserves_originals(tmp_path):
    with NotificationStore(tmp_path / "queue") as queue:
        e = event(category="DATA_UNAVAILABLE")
        queue.enqueue(e, fingerprint(e), whatsapp=False)
        e2 = event(2000, "DATA_UNAVAILABLE")
        queue.enqueue(e2, fingerprint(e2), whatsapp=False)
        assert queue.summary()["states"] == {"COALESCED": 1, "PENDING": 1}
        first = queue.claim(1000)
        queue.finish(first, "FAILED", 1000)
        assert queue.claim(60999) is None
        second = queue.claim(61000)
        queue.finish(second, "UNKNOWN", 61000)
        assert queue.claim(360999) is None
        third = queue.claim(361000)
        queue.finish(third, "FAILED", 361000)
        assert queue.claim(9999999) is None
        assert queue.summary()["attempts"] == 3
        assert queue.summary()["states"] == {"COALESCED": 1, "FAILED": 1}


def test_global_rate_and_payload_hash_before_insert(tmp_path):
    with NotificationStore(tmp_path / "queue") as queue:
        e = event()
        queue.enqueue(e, fingerprint(e), whatsapp=False)
        second = event(2000)
        queue.enqueue(second, fingerprint(second), whatsapp=False)
        first = queue.claim(1000)
        queue.finish(first, "CONFIRMED", 1001)
        assert queue.claim(1999) is None
        assert queue.claim(2000)["event"]["event_id"] == second["event_id"]
        with pytest.raises(PaperError, match="NOTIFICATION_SOURCE_DIVERGENCE"):
            queue.enqueue({**e, "reason": "changed"}, fingerprint(e), whatsapp=False)


def test_queue_backup_restores_unknown_lease_and_independent_writer(tmp_path):
    restored = tmp_path / "restored"
    restored.mkdir()
    with NotificationStore(tmp_path / "queue") as queue:
        e = event()
        queue.enqueue(e, fingerprint(e), whatsapp=False)
        queue.claim(1000)
        with pytest.raises(PaperError, match="WRITER_BUSY"):
            with NotificationStore(tmp_path / "queue"):
                pass
        queue.backup(restored / "notifications.sqlite3")
    with NotificationStore(restored) as queue:
        queue.recover(61000)
        assert queue.summary()["unknown_attempts"] == 1
        claim = queue.claim(61000)
        assert claim["duplicate_possible"] and claim["attempt_no"] == 2
        queue.finish(claim, "CONFIRMED", 61001)
        assert queue.claim(9999999) is None


def test_shorter_whatsapp_timeout_is_applied():
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(
            200, json={"status": "SESSAO_ABERTA" if request.method == "GET" else "enviado"}
        )

    cfg = settings().model_copy(update={"whatsapp_timeout_seconds": 1})
    assert (
        LocalSender(cfg, transport=httpx.MockTransport(handle)).send(event(), "whatsapp")
        == "CONFIRMED"
    )
    assert all(item["read"] == 1 and item["write"] == 1 for item in seen)


def test_second_abandoned_mail_attempt_keeps_three_hundred_second_backoff(tmp_path):
    with NotificationStore(tmp_path / "queue") as queue:
        e = event()
        queue.enqueue(e, fingerprint(e), whatsapp=False)
        first = queue.claim(1000)
        queue.finish(first, "FAILED", 1000)
        second = queue.claim(61000)
        assert second["attempt_no"] == 2
        # Lease expiration makes the outcome UNKNOWN; it cannot shorten retry2's delay.
        assert queue.claim(121000) is None
        assert queue.summary()["unknown_attempts"] == 1
        assert queue.claim(360999) is None
        assert queue.claim(361000)["attempt_no"] == 3


@pytest.mark.parametrize("url", ["http://127.0.0.1:80", "https://localhost:443"])
def test_explicit_default_port_preserves_exact_local_allowlist(url):
    def handle(request):
        assert request.url.path == "/api/v1/mail/send"
        assert request.url.host in {"127.0.0.1", "localhost"}
        return httpx.Response(200, json={"message": "E-mail enviado com sucesso."})

    cfg = settings().model_copy(update={"mail_sender_url": url})
    assert (
        LocalSender(cfg, transport=httpx.MockTransport(handle)).send(event(), "mail") == "CONFIRMED"
    )
