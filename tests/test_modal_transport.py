"""Offline regressions for the WSL/Zscaler failures, without real credentials."""

import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import modal
import pytest

from box import modal_transport as transport
from box import state


@pytest.fixture
def configured(monkeypatch):
    # Register originals with monkeypatch before the integration replaces them.
    for module, name in (
        (transport.grpc_utils, "ModalChannel"),
        (transport.router, "ModalChannel"),
        (transport.router, "_connect_channel"),
        (transport.http_utils, "_http_client_with_tls"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(transport, "_DNS_CACHE", {})
    monkeypatch.setattr(transport, "_dns_notice_shown", False)
    monkeypatch.delenv("BOX_DNS_OVER_HTTPS", raising=False)
    transport.configure_transport()
    return transport.grpc_utils.ModalChannel


def test_system_trust_and_h2_apply_to_both_channel_factories(configured, monkeypatch):
    paths = ssl.get_default_verify_paths()
    get_paths = Mock(return_value=paths)
    monkeypatch.setattr(ssl, "get_default_verify_paths", get_paths)
    original_alpn = ssl.SSLContext.set_alpn_protocols
    protocols = []

    def alpn(context, values):
        protocols.append(values)
        original_alpn(context, values)

    monkeypatch.setattr(ssl.SSLContext, "set_alpn_protocols", alpn)

    async def check():
        assert configured is transport.router.ModalChannel
        for factory in (transport.grpc_utils.ModalChannel, transport.router.ModalChannel):
            channel = factory("api.modal.com", 443, ssl=True)
            try:
                assert channel._ssl.check_hostname
                assert channel._ssl.verify_mode == ssl.CERT_REQUIRED
                assert channel._ssl.minimum_version >= ssl.TLSVersion.TLSv1_2
            finally:
                channel.close()
        assert get_paths.call_count == 2
        assert protocols and all(value == ["h2"] for value in protocols)

    asyncio.run(check())


def test_explicit_router_context_gets_h2_without_disabling_verification(configured, monkeypatch):
    context = ssl.create_default_context()
    alpn = Mock()
    monkeypatch.setattr(context, "set_alpn_protocols", alpn)

    async def check():
        channel = configured("task.example.modal.host", 443, ssl=context)
        try:
            assert channel._ssl is context
            alpn.assert_called_once_with(["h2"])
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            plain = configured("localhost", 80, ssl=False)
            assert plain._ssl is None
            plain.close()
        finally:
            channel.close()

    asyncio.run(check())


def test_explicit_verify_paths_are_retained(configured, monkeypatch):
    paths = ssl.get_default_verify_paths()
    monkeypatch.setattr(ssl, "get_default_verify_paths", Mock(side_effect=AssertionError))

    async def check():
        channel = configured("api.modal.com", 443, ssl=paths)
        assert channel._ssl.verify_mode == ssl.CERT_REQUIRED
        channel.close()

    asyncio.run(check())


def test_configuration_is_idempotent(configured):
    http = transport.http_utils._http_client_with_tls
    router_connect = transport.router._connect_channel
    transport.configure_transport()
    assert transport.grpc_utils.ModalChannel is configured
    assert transport.http_utils._http_client_with_tls is http
    assert transport.router._connect_channel is router_connect


def test_blob_http_adds_host_roots_and_preserves_sdk_connector(configured, monkeypatch):
    loaded_contexts = []

    def load_roots(context, *args):
        loaded_contexts.append(context)

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", load_roots)
    # No real HTTP request. Exercise the actual SDK session/connector factory.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    async def check():
        async with transport.http_utils._http_client_with_tls(timeout=8) as session:
            assert session.timeout.total == 8
            assert session.connector._ssl.verify_mode == ssl.CERT_REQUIRED
            assert session.connector._ssl.check_hostname
            assert loaded_contexts[-1] is session.connector._ssl

    asyncio.run(check())


class Response:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self.data


def fake_dns(monkeypatch, data):
    session = Response(None)
    session.get = Mock(return_value=Response(data))
    factory = Mock(return_value=session)
    monkeypatch.setattr(transport.http_utils, "_http_client_with_tls", factory)
    return factory, session.get


def test_dns_fallback_is_bounded_cached_and_only_sends_hostname(configured, monkeypatch, capsys):
    system = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, request = fake_dns(
        monkeypatch,
        {"Status": 0, "Answer": [{"type": 1, "data": "54.197.211.33", "TTL": 60}]},
    )

    async def check():
        for _ in range(2):
            assert await transport._sandbox_addresses("task.test.modal.host", 443) == [
                "54.197.211.33"
            ]

    asyncio.run(check())
    system.assert_awaited_once()
    factory.assert_called_once_with(timeout=8)
    request.assert_called_once_with(
        "https://dns.google/resolve",
        params={"name": "task.test.modal.host", "type": "A"},
        allow_redirects=False,
    )
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.count("using DNS-over-HTTPS") == 1


@pytest.mark.parametrize("mode", ["force", " FORCE "])
def test_forced_doh_skips_system_dns_and_reuses_cache(configured, monkeypatch, capsys, mode):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", mode)
    system = AsyncMock(side_effect=AssertionError("system DNS must not be attempted"))
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, request = fake_dns(
        monkeypatch,
        {"Status": 0, "Answer": [{"type": 1, "data": "54.197.211.33", "TTL": 60}]},
    )

    async def check():
        for _ in range(2):
            assert await transport._sandbox_addresses("task.test.modal.host", 443) == [
                "54.197.211.33"
            ]

    asyncio.run(check())
    system.assert_not_called()
    factory.assert_called_once_with(timeout=8)
    request.assert_called_once_with(
        "https://dns.google/resolve",
        params={"name": "task.test.modal.host", "type": "A"},
        allow_redirects=False,
    )
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.count("BOX_DNS_OVER_HTTPS=force") == 1
    assert "skipping system DNS" in out.err
    assert "could not resolve" not in out.err


def test_forced_doh_refreshes_expired_cache_without_system_dns(configured, monkeypatch):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    transport._DNS_CACHE["task.test.modal.host"] = (0, ["54.197.211.34"])
    system = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, _ = fake_dns(
        monkeypatch,
        {"Status": 0, "Answer": [{"type": 1, "data": "54.197.211.33", "TTL": 60}]},
    )
    assert asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443)) == [
        "54.197.211.33"
    ]
    system.assert_not_called()
    factory.assert_called_once()


@pytest.mark.parametrize("failure", [OSError("HTTPS failed"), TimeoutError("HTTPS timed out")])
def test_forced_doh_failure_never_reverts_to_system_dns(configured, monkeypatch, failure):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    system = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, _ = fake_dns(monkeypatch, {})
    factory.side_effect = failure
    with pytest.raises(type(failure), match="HTTPS"):
        asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443))
    system.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        {"Status": 2},
        {"Status": 0, "Answer": []},
        {"Status": 0, "Answer": [{"type": 1, "data": "127.0.0.1", "TTL": 60}]},
    ],
)
def test_forced_doh_keeps_response_validation(configured, monkeypatch, data):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    system = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(transport, "_system_addresses", system)
    fake_dns(monkeypatch, data)
    with pytest.raises(transport.ModalConnectionError, match="DNS-over-HTTPS"):
        asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443))
    system.assert_not_called()


@pytest.mark.parametrize("host", ["private.example", "api.modal.com", "task.modal.host.example"])
def test_force_never_sends_other_names_to_public_resolver(configured, monkeypatch, host):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    system = AsyncMock(return_value=["192.0.2.1"])
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, _ = fake_dns(monkeypatch, {})
    assert asyncio.run(transport._sandbox_addresses(host, 443)) == ["192.0.2.1"]
    system.assert_awaited_once_with(host, 443)
    factory.assert_not_called()


def test_opt_out_ignores_cache_created_in_force_mode(configured, monkeypatch):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    system = AsyncMock(return_value=["192.0.2.1"])
    monkeypatch.setattr(transport, "_system_addresses", system)
    factory, _ = fake_dns(
        monkeypatch,
        {"Status": 0, "Answer": [{"type": 1, "data": "54.197.211.33", "TTL": 60}]},
    )
    assert asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443)) == [
        "54.197.211.33"
    ]
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "0")
    assert asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443)) == ["192.0.2.1"]
    system.assert_awaited_once()
    factory.assert_called_once()  # The second lookup must not use HTTPS or its cached answer.


@pytest.mark.parametrize(
    "host,enabled",
    [
        ("task.test.modal.host", "0"),
        ("private.example", "1"),
        ("private.example", "force"),
    ],
)
def test_dns_opt_out_and_non_modal_names_never_use_public_resolver(
    configured, monkeypatch, host, enabled
):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", enabled)
    monkeypatch.setattr(transport, "_system_addresses", AsyncMock(side_effect=OSError))
    factory, _ = fake_dns(monkeypatch, {})
    with pytest.raises(transport.ModalConnectionError, match="System DNS"):
        asyncio.run(transport._sandbox_addresses(host, 443))
    factory.assert_not_called()


def test_working_system_dns_does_not_use_public_resolver(configured, monkeypatch):
    monkeypatch.setattr(transport, "_system_addresses", AsyncMock(return_value=["192.0.2.1"]))
    factory, _ = fake_dns(monkeypatch, {})
    assert asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443)) == ["192.0.2.1"]
    factory.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_public_dns_fallback_rejects_nonpublic_addresses(configured, monkeypatch, address):
    monkeypatch.setattr(transport, "_system_addresses", AsyncMock(side_effect=TimeoutError))
    fake_dns(monkeypatch, {"Status": 0, "Answer": [{"type": 1, "data": address, "TTL": 60}]})
    with pytest.raises(transport.ModalConnectionError, match="non-public"):
        asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443))


def test_dns_cache_expires(configured, monkeypatch):
    transport._DNS_CACHE["task.test.modal.host"] = (0, ["54.197.211.33"])
    system = AsyncMock(return_value=["192.0.2.2"])
    monkeypatch.setattr(transport, "_system_addresses", system)
    assert asyncio.run(transport._sandbox_addresses("task.test.modal.host", 443)) == ["192.0.2.2"]
    system.assert_awaited_once()


@pytest.mark.parametrize("fallback,budget", [("1", 3.0), ("0", 3.0)])
def test_system_dns_budget_preserves_opt_out(monkeypatch, fallback, budget):
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(b'["192.0.2.1"]', b""))
    )
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", fallback)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    assert asyncio.run(transport._system_addresses("task.test.modal.host", 443)) == ["192.0.2.1"]
    timeout.assert_called_once_with(budget)


def test_stuck_system_dns_child_is_killed_and_reaped(monkeypatch):
    process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock())

    async def stuck():
        await asyncio.Event().wait()

    process.communicate = stuck
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(transport, "DNS_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        asyncio.run(transport._system_addresses("task.test.modal.host", 443))
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


def test_dialing_resolved_ip_preserves_tls_hostname(configured, monkeypatch):
    monkeypatch.setattr(transport, "get_proxy_url", lambda *a, **kw: None)
    monkeypatch.setattr(transport, "_sandbox_addresses", AsyncMock(return_value=["54.197.211.33"]))

    async def check():
        connection = AsyncMock(return_value=(object(), "protocol"))
        monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", connection)
        channel = configured("task.test.modal.host", 443, ssl=True)
        try:
            assert await channel._open_connection() == "protocol"
            args, kwargs = connection.call_args
            assert args[1:] == ("54.197.211.33", 443)
            assert kwargs["server_hostname"] == "task.test.modal.host"
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        finally:
            channel.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "host,proxy", [("api.modal.com", None), ("task.test.modal.host", "http://proxy:80")]
)
@pytest.mark.parametrize("mode", ["1", "force"])
def test_proxy_and_control_plane_keep_sdk_routing(configured, monkeypatch, host, proxy, mode):
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", mode)
    original = configured.__mro__[1]
    open_connection = AsyncMock(return_value="sdk-protocol")
    monkeypatch.setattr(original, "_open_connection", open_connection)
    monkeypatch.setattr(transport, "get_proxy_url", lambda *a, **kw: proxy)
    resolver = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(transport, "_sandbox_addresses", resolver)

    async def check():
        channel = configured(host, 443, ssl=True)
        try:
            assert await channel._open_connection() == "sdk-protocol"
        finally:
            channel.close()

    asyncio.run(check())
    open_connection.assert_awaited_once()
    resolver.assert_not_awaited()


def test_forced_doh_dials_ip_but_keeps_original_tls_hostname(configured, monkeypatch):
    host = "Task.Test.Modal.Host."
    monkeypatch.setenv("BOX_DNS_OVER_HTTPS", "force")
    monkeypatch.setattr(transport, "get_proxy_url", lambda *a, **kw: None)
    system = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(transport, "_system_addresses", system)
    fake_dns(
        monkeypatch,
        {"Status": 0, "Answer": [{"type": 1, "data": "54.197.211.33", "TTL": 60}]},
    )

    async def check():
        connection = AsyncMock(return_value=(object(), "protocol"))
        monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", connection)
        channel = configured(host, 443, ssl=True)
        try:
            assert await channel._open_connection() == "protocol"
            args, kwargs = connection.call_args
            assert args[1:] == ("54.197.211.33", 443)
            assert kwargs["server_hostname"] == host
            assert kwargs["ssl"].check_hostname
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        finally:
            channel.close()

    asyncio.run(check())
    system.assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "hello"])
def test_preflight_cancels_stalled_connection_or_authentication(monkeypatch, stage):
    cancelled = []

    async def stuck():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    client = SimpleNamespace(hello=stuck if stage == "hello" else AsyncMock())
    monkeypatch.setattr(transport, "configure_transport", lambda: None)
    monkeypatch.setattr(transport, "CONNECT_TIMEOUT", 0.01)
    monkeypatch.setattr(
        transport._Client,
        "from_env",
        stuck if stage == "connect" else AsyncMock(return_value=client),
    )
    with pytest.raises(transport.ModalConnectionError, match="No box operation was started"):
        transport.connect()
    assert cancelled == [True]


def test_preflight_authenticates_and_preserves_auth_errors(monkeypatch):
    hello = AsyncMock(side_effect=modal.exception.AuthError("invalid credentials"))
    monkeypatch.setattr(transport, "configure_transport", lambda: None)
    monkeypatch.setattr(
        transport._Client, "from_env", AsyncMock(return_value=SimpleNamespace(hello=hello))
    )
    with pytest.raises(modal.exception.AuthError):
        transport.connect()
    hello.assert_awaited_once()


def test_router_connect_deadline_cancels_sdk_retries(monkeypatch, configured):
    monkeypatch.setattr(transport, "ROUTER_CONNECT_TIMEOUT", 0.01)
    channel = SimpleNamespace(__connect__=AsyncMock(side_effect=lambda: None))

    async def stuck():
        await asyncio.Event().wait()

    channel.__connect__ = stuck
    with pytest.raises(transport.ModalConnectionError, match="sandbox connection timed out"):
        asyncio.run(transport.router._connect_channel(channel))


def test_preflight_fails_before_any_remote_registry_access(monkeypatch):
    connect = Mock(side_effect=transport.ModalConnectionError("offline"))
    lookup = Mock(side_effect=AssertionError)
    monkeypatch.setattr(state, "connect", connect)
    monkeypatch.setattr(state.modal.Dict, "from_name", lookup)
    with pytest.raises(transport.ModalConnectionError):
        state.Registry()
    lookup.assert_not_called()
    # Injected registries remain completely offline, including falsy ones.
    assert state.Registry({}).data == {}
    connect.assert_called_once()
