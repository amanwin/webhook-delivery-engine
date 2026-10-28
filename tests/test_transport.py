"""Behaviour of the real UrllibTransport against misbehaving local servers.

These tests open real sockets on 127.0.0.1 so that the error classification is checked
against what ``urllib`` actually raises, not against what we assume it raises.
"""

import socket
import threading
import time
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected

import pytest

from webhook_delivery import (
    RetryPolicy,
    TransportError,
    UrllibTransport,
    WebhookDeliveryEngine,
    WebhookEvent,
)


def _drain_request(conn):
    conn.settimeout(2)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(rest) < length:
        rest += conn.recv(4096)
    return data


@pytest.fixture
def raw_server():
    """Serve each connection with the next scripted handler and return the URL."""
    servers = []

    def start(*handlers):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sock.settimeout(0.05)
        served = []
        stop = threading.Event()

        def accept():
            while not stop.is_set():
                try:
                    return sock.accept()[0]
                except TimeoutError:
                    continue
            return None

        def run():
            for handler in handlers:
                conn = accept()
                if conn is None:
                    return
                try:
                    _drain_request(conn)
                    # Record before responding: the client may finish and assert before
                    # this thread is scheduled again after sendall() returns.
                    served.append(handler.__name__)
                    handler(conn)
                finally:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    conn.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        servers.append((sock, thread, stop))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/hook", served

    yield start
    for sock, thread, stop in servers:
        stop.set()
        thread.join(timeout=2)
        sock.close()


def close_without_reply(conn):
    pass


def garbage_status_line(conn):
    conn.sendall(b"NOT HTTP AT ALL\r\n\r\n")


def truncated_200(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")


def truncated_503(conn):
    conn.sendall(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 100\r\n\r\nshort")


def truncated_400(conn):
    conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 100\r\n\r\nshort")


def hang(conn):
    # Never answer; return once the client gives up and closes its side.
    conn.settimeout(5)
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


def ok_200(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


def status_503(conn):
    conn.sendall(
        b"HTTP/1.1 503 Unavailable\r\nContent-Length: 4\r\nRetry-After: 0\r\n"
        b"Connection: close\r\n\r\ndown"
    )


def status_404(conn):
    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def event():
    return WebhookEvent("evt-1", "order.created", {"id": 1})


# --- Classification of individual failures ------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "cause"),
    [
        (close_without_reply, RemoteDisconnected),
        (garbage_status_line, BadStatusLine),
        (truncated_200, IncompleteRead),
    ],
)
def test_broken_responses_become_retryable_transport_errors(raw_server, handler, cause):
    url, _ = raw_server(handler)
    with pytest.raises(TransportError) as info:
        UrllibTransport().post(url, b"{}", {}, 2.0)
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__, cause)


def test_read_timeout_is_a_retryable_transport_error(raw_server):
    url, _ = raw_server(hang)
    started = time.monotonic()
    with pytest.raises(TransportError) as info:
        UrllibTransport().post(url, b"{}", {}, 0.2)
    assert time.monotonic() - started < 1.0
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__, TimeoutError)


def test_connection_refused_is_a_retryable_transport_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing is listening on this port any more
    with pytest.raises(TransportError) as info:
        UrllibTransport().post(f"http://127.0.0.1:{port}/", b"{}", {}, 2.0)
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__.reason, ConnectionRefusedError)


@pytest.mark.parametrize(("handler", "status"), [(truncated_503, 503), (truncated_400, 400)])
def test_truncated_error_response_keeps_its_status(raw_server, handler, status):
    # The status is what the retry policy classifies on, so it must survive a broken body.
    url, _ = raw_server(handler)
    response = UrllibTransport().post(url, b"{}", {}, 2.0)
    assert response.status_code == status
    assert response.body in (b"", b"short")  # best effort


# --- End-to-end retry behaviour through the real transport --------------------------------


def engine(max_attempts):
    return WebhookDeliveryEngine(
        timeout=0.5, retry=RetryPolicy(max_attempts=max_attempts, delay=0.0)
    )


@pytest.mark.parametrize(
    "failure",
    [close_without_reply, garbage_status_line, truncated_200, hang, status_503],
    ids=lambda h: h.__name__,
)
def test_delivery_succeeds_after_one_transient_failure(raw_server, failure):
    url, served = raw_server(failure, ok_200)

    result = engine(max_attempts=3).deliver(url, event())

    assert result.delivered is True
    assert result.status_code == 200
    assert result.response_body == b"ok"
    assert result.attempts == 2
    assert served == [failure.__name__, "ok_200"]


def test_transport_errors_exhaust_retries_and_raise_the_last_error(raw_server):
    url, served = raw_server(close_without_reply, garbage_status_line, truncated_200, ok_200)

    with pytest.raises(TransportError) as info:
        engine(max_attempts=3).deliver(url, event())

    assert isinstance(info.value.__cause__, IncompleteRead)  # the third and final attempt
    assert served == ["close_without_reply", "garbage_status_line", "truncated_200"]


def test_server_errors_exhaust_retries_and_return_the_last_response(raw_server):
    url, served = raw_server(status_503, status_503, status_503, ok_200)

    result = engine(max_attempts=3).deliver(url, event())

    assert result.delivered is False
    assert result.status_code == 503
    assert result.response_body == b"down"
    assert result.attempts == 3
    assert served == ["status_503"] * 3


def test_client_error_is_not_retried_even_with_attempts_remaining(raw_server):
    url, served = raw_server(status_404, ok_200)

    result = engine(max_attempts=3).deliver(url, event())

    assert result.delivered is False
    assert result.status_code == 404
    assert result.attempts == 1
    assert served == ["status_404"]


def test_without_retry_policy_a_transient_failure_surfaces_immediately(raw_server):
    url, served = raw_server(close_without_reply, ok_200)

    with pytest.raises(TransportError):
        WebhookDeliveryEngine(timeout=0.5).deliver(url, event())
    assert served == ["close_without_reply"]
