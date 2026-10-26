"""Local notification HTTP contracts; never shares the Binance request guard."""

import html
import json
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from cripto_core.errors import PaperError
from cripto_core.serialization import canonical

from cripto_bot.adapters.continuous_config import NotificationInput, endpoint


class LocalSender:
    def __init__(
        self,
        settings: NotificationInput,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.monotonic = monotonic

    def send(
        self, event: dict[str, object], channel: str, *, duplicate_possible: bool = False
    ) -> str:
        if channel not in {"mail", "whatsapp"}:
            raise PaperError("INVALID_CHANNEL")
        if not self.settings.enabled:
            return "FAILED"
        if channel == "whatsapp" and not self.settings.whatsapp_enabled:
            return "FAILED"
        base = endpoint(
            self.settings.mail_sender_url
            if channel == "mail"
            else self.settings.whatsapp_notify_url
        )
        expected = urlsplit(base)
        started = self.monotonic()

        def guard(request: httpx.Request) -> None:
            u = request.url
            port = u.port
            if port is None:
                port = 443 if u.scheme == "https" else 80
            allowed = (
                {("POST", "/api/v1/mail/send")}
                if channel == "mail"
                else {("GET", "/whatsapp/session/status"), ("POST", "/whatsapp/messages/send")}
            )
            if (
                (request.method, u.path) not in allowed
                or u.scheme != expected.scheme
                or u.host != expected.hostname
                or port != expected.port
                or u.username
                or u.password
                or u.query
                or any(
                    k in request.headers
                    for k in ["authorization", "x-mbx-apikey", "proxy-authorization"]
                )
            ):
                raise PaperError("FORBIDDEN_NOTIFICATION_REQUEST")

        def request(
            client: httpx.Client, method: str, path: str, payload: dict[str, object] | None = None
        ) -> tuple[int, object]:
            remaining = 10 - (self.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            with client.stream(
                method,
                base + path,
                json=payload,
                timeout=httpx.Timeout(
                    min(
                        self.settings.whatsapp_timeout_seconds if channel == "whatsapp" else 3,
                        remaining,
                    ),
                    connect=min(1, remaining),
                    pool=min(1, remaining),
                ),
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if self.monotonic() - started > 10:
                        raise TimeoutError
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise ValueError
                return response.status_code, json.loads(body)

        posted = False
        try:
            with httpx.Client(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                verify=True,
                event_hooks={"request": [guard]},
            ) as client:
                if channel == "whatsapp":
                    status, body = request(client, "GET", "/whatsapp/session/status")
                    if (
                        status != 200
                        or not isinstance(body, dict)
                        or body.get("status") != "SESSAO_ABERTA"
                    ):
                        return "FAILED"
                message = canonical(event)
                if duplicate_possible:
                    message = "Entrega anterior desconhecida; possível duplicação. " + message
                payload: dict[str, object]
                if channel == "mail":
                    if self.settings.mail_content_type == "HTML":
                        message = "<pre>" + html.escape(message) + "</pre>"
                    payload = {
                        "to": self.settings.mail_to,
                        "subject": (
                            f"Cripto PAPER {event['category']} {str(event['event_id'])[:12]}"
                        ),
                        "body": message,
                        "message_type": self.settings.mail_content_type,
                    }
                    path = "/api/v1/mail/send"
                else:
                    payload = {"contact": self.settings.whatsapp_contact, "message": message}
                    path = "/whatsapp/messages/send"
                if len(canonical(payload).encode()) > 65536:
                    return "FAILED"
                posted = True
                status, body = request(client, "POST", path, payload)
                if 400 <= status < 500:
                    return "FAILED"
                if status != 200 or not isinstance(body, dict):
                    return "UNKNOWN"
                valid = (
                    body.get("message") == "E-mail enviado com sucesso."
                    if channel == "mail"
                    else body.get("status") == "enviado"
                )
                return "CONFIRMED" if valid else "UNKNOWN"
        except (httpx.HTTPError, TimeoutError, ValueError):
            return "UNKNOWN" if posted else "FAILED"
