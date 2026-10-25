import httpx
import pytest
from cripto_bot.adapters.binance.public import HOST, MAX_BYTES, Budget, PublicSource
from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import MarketError


class FakeClock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def client(handler, **kwargs):
    clock = FakeClock()
    source = PublicSource(
        budget=Budget(monotonic=clock.now),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        jitter=lambda: 0,
        **kwargs,
    )
    return source, clock


def test_closed_surface_and_params(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://SYNTHETIC_SECRET.invalid")
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url).startswith(HOST + "/api/v3/")
        assert request.method == "GET" and "authorization" not in request.headers
        assert "x-mbx-apikey" not in request.headers
        return httpx.Response(
            200, json={}, headers={"X-MBX-USED-WEIGHT-1M": "7", "secret": "hidden"}
        )

    source, _ = client(handler)
    try:
        assert source.server_time().headers == {"x-mbx-used-weight-1m": "7"}
        source.metadata()
        source.quote()
        source.klines(0, 899999, 1000)
        assert {r.url.path for r in requests} == {
            "/api/v3/time",
            "/api/v3/exchangeInfo",
            "/api/v3/ticker/bookTicker",
            "/api/v3/klines",
        }
        assert requests[-1].url.params["endTime"] == "899999"
        assert requests[-1].url.params["timeZone"] == "0"
        assert not hasattr(source, "request")
        with pytest.raises(MarketError, match="FORBIDDEN"):
            source._get("/api/v3/order", {})
        with pytest.raises(MarketError, match="INVALID_REQUEST"):
            source.klines(True, 1, 1001)
    finally:
        source.close()


@pytest.mark.parametrize("failure", [500, 503, "timeout"])
def test_transient_retry_then_success(failure):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("SYNTHETIC_SECRET")
            return httpx.Response(failure)
        return httpx.Response(200, json={})

    source, clock = client(handler)
    assert source.server_time().json() == {}
    assert len(attempts) == 3 and clock.value == 1.5
    source.close()


@pytest.mark.parametrize("code", [400, 403, 404, 301, 302])
def test_no_retry_or_redirect(code):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(code, headers={"Location": "https://invalid.invalid"})

    source, _ = client(handler)
    with pytest.raises(MarketError, match="HTTP_REJECTED"):
        source.server_time()
    assert len(attempts) == 1
    source.close()


def test_retry_exhaustion():
    source, clock = client(lambda _: httpx.Response(500))
    with pytest.raises(MarketError, match="RETRIES_EXHAUSTED"):
        source.server_time()
    assert clock.value == 1.5
    source.close()


@pytest.mark.parametrize(
    "status,delay,expected",
    [
        (429, "2", None),
        (429, "100", "DEADLINE"),
        (418, "5", "IP_BANNED"),
        (429, "invalid", "DEADLINE"),
        (429, "nan", "DEADLINE"),
    ],
)
def test_rate_limits(status, delay, expected):
    calls = []
    blocked = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(status, headers={"Retry-After": delay})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    source, clock = client(handler, blocked_until=blocked.append, wall_time=lambda: 1000)
    if expected:
        with pytest.raises(MarketError, match=expected):
            source.server_time()
        assert len(calls) == 1 and clock.value == 0
    else:
        source.server_time()
        assert clock.value == 2 and len(calls) == 2
    assert blocked
    source.close()


def test_budget_and_response_size():
    clock = FakeClock()
    budget = Budget(monotonic=clock.now, seconds=1)
    assert budget.remaining() == 1
    clock.value = 1
    with pytest.raises(MarketError, match="DEADLINE"):
        budget.remaining()
    source, _ = client(lambda _: httpx.Response(200, content=b"x" * (MAX_BYTES + 1)))
    with pytest.raises(MarketError, match="TOO_LARGE"):
        source.server_time()
    source.close()


@pytest.mark.parametrize("body", [b"{", b"NaN", b"Infinity", b"\xff"])
def test_bad_json(body):
    with pytest.raises(MarketError, match="INVALID_JSON"):
        RawResponse("test", {}, body, "").json()


@pytest.mark.parametrize(
    "method,url,headers",
    [
        ("POST", HOST + "/api/v3/time", {}),
        ("DELETE", HOST + "/api/v3/time", {}),
        ("GET", "https://other.invalid/api/v3/time", {}),
        ("GET", HOST + "/api/v3/order", {}),
        ("GET", HOST + "/api/v3/time?signature=bad", {}),
        ("GET", HOST + "/api/v3/time", {"X-MBX-APIKEY": "SYNTHETIC_SECRET"}),
        ("GET", HOST + "/api/v3/exchangeInfo?symbol=ETHUSDT", {}),
        (
            "GET",
            HOST
            + "/api/v3/klines?symbol=BTCUSDT&interval=1m&timeZone=0&startTime=0&endTime=1&limit=1",
            {},
        ),
        ("GET", HOST + "/api/v3/time?a=1&a=2", {}),
    ],
)
def test_request_guard_prevents_forbidden_surface(method, url, headers):
    def forbidden(request):
        pytest.fail("requisicao proibida alcancou transporte")

    source, _ = client(forbidden)
    with pytest.raises(MarketError, match="FORBIDDEN_REQUEST"):
        source._client.request(method, url, headers=headers)
    source.close()


def test_invalid_json_is_not_retried():
    calls = []
    source, _ = client(lambda request: calls.append(request) or httpx.Response(200, content=b"{"))
    with pytest.raises(MarketError, match="INVALID_JSON"):
        source.server_time().json()
    assert len(calls) == 1
    source.close()


def test_read_deadline_and_bad_compression():
    clock = FakeClock()

    def slow(request):
        clock.value += 91
        return httpx.Response(200, content=b"{}")

    source = PublicSource(budget=Budget(monotonic=clock.now), transport=httpx.MockTransport(slow))
    with pytest.raises(MarketError, match="DEADLINE"):
        source.server_time()
    source.close()

    def invalid(request):
        raise httpx.DecodingError("SYNTHETIC_SECRET")

    source, _ = client(invalid)
    with pytest.raises(MarketError, match="INVALID_ENCODING"):
        source.server_time()
    source.close()
