"""Modal 1.6 transport compatibility for enterprise TLS and WSL DNS.

Private SDK integration is isolated here and covered by regression tests. Keep
verification enabled, preserve proxy routing, and never modify host networking.
"""

import asyncio
import ipaddress
import json
import os
import ssl
import sys
import time
from functools import wraps

from modal._utils import grpc_utils, http_utils
from modal._utils.async_utils import synchronizer
from modal._utils.proxy_support import get_proxy_url
from modal.client import _Client
from modal.sandbox import _task_command_router_client as router

CONNECT_TIMEOUT = 20.0
ROUTER_CONNECT_TIMEOUT = 30.0
DNS_TIMEOUT = 3.0
_DNS_CACHE = {}
_dns_notice_shown = False


class ModalConnectionError(RuntimeError):
    """A bounded Modal connectivity check failed."""


async def _system_addresses(host, port):
    # getaddrinfo runs in a thread in asyncio; cancelling it leaves that thread
    # stuck in WSL DNS and delays CLI exit. A small isolated child can be reaped.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import json,socket,sys; "
        "print(json.dumps(list(dict.fromkeys(a[4][0] for a in "
        "socket.getaddrinfo(sys.argv[1],sys.argv[2],type=socket.SOCK_STREAM)))))",
        host,
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(DNS_TIMEOUT):
            stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise OSError("System DNS lookup failed")
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _is_sandbox_host(host):
    return host.lower().removesuffix(".").endswith(".modal.host")


async def _sandbox_addresses(host, port):
    """System DNS, automatic DoH fallback, or explicit DoH for Modal workers."""
    global _dns_notice_shown
    mode = os.environ.get("BOX_DNS_OVER_HTTPS", "1").strip().lower()
    eligible = _is_sandbox_host(host)
    fallback = mode != "0" and eligible
    forced = mode == "force" and eligible
    cached = _DNS_CACHE.get(host) if fallback else None
    if cached and cached[0] > time.monotonic():
        return cached[1]
    if not forced:
        try:
            return await _system_addresses(host, port)
        except (OSError, TimeoutError) as exc:
            if not fallback:
                raise ModalConnectionError(
                    "System DNS could not resolve the Modal sandbox hostname. "
                    "Check your DNS/VPN or enable BOX_DNS_OVER_HTTPS=1."
                ) from exc

    # Only public sandbox DNS names are sent, never API tokens or exec payloads.
    # A finite HTTP timeout also bounds TLS and response reads through Zscaler.
    async with http_utils._http_client_with_tls(timeout=8) as session:
        async with session.get(
            "https://dns.google/resolve",
            params={"name": host, "type": "A"},
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            data = await response.json()
    answers = data.get("Answer", []) if data.get("Status") == 0 else []
    addresses = []
    ttl = 60
    for answer in answers:
        if answer.get("type") != 1:
            continue
        address = ipaddress.ip_address(answer["data"])
        if address.version != 4 or not address.is_global:
            raise ModalConnectionError("DNS-over-HTTPS returned a non-public sandbox address")
        addresses.append(str(address))
        ttl = min(ttl, max(0, int(answer.get("TTL", 0))))
    if not addresses:
        raise ModalConnectionError("DNS-over-HTTPS did not resolve the Modal sandbox hostname")
    _DNS_CACHE[host] = (time.monotonic() + ttl, addresses)
    if not _dns_notice_shown:
        notice = (
            "box: BOX_DNS_OVER_HTTPS=force: using DNS-over-HTTPS (dns.google); "
            "skipping system DNS for Modal sandbox hosts."
            if forced
            else "box: System DNS could not resolve Modal sandbox hosts; using DNS-over-HTTPS "
            "(dns.google). Set BOX_DNS_OVER_HTTPS=0 to disable this fallback."
        )
        print(notice, file=sys.stderr)
        _dns_notice_shown = True
    return addresses


def configure_transport():
    """Apply once, before Modal creates channels; no network I/O here."""
    if getattr(grpc_utils.ModalChannel, "_box_system_trust", False):
        return

    class SystemTrustChannel(grpc_utils.ModalChannel):
        _box_system_trust = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self._ssl is not None:
                # The task router passes its own verified SSLContext, but Modal
                # 1.6.3 omits ALPN there. Zscaler stalls that TLS handshake.
                self._ssl.set_alpn_protocols(["h2"])

        def _get_default_ssl_context(self, *, verify_paths=None):
            # Retain grpclib's TLS minimum/ciphers, respecting SSL_CERT_FILE/DIR.
            return super()._get_default_ssl_context(
                verify_paths=(
                    verify_paths if verify_paths is not None else ssl.get_default_verify_paths()
                )
            )

        async def _open_connection(self):
            host = self._host
            if (
                self._path is not None
                or not _is_sandbox_host(host)
                or get_proxy_url(host, use_ssl=self._ssl is not None) is not None
            ):
                # A configured proxy owns DNS resolution; do not bypass it.
                return await super()._open_connection()
            addresses = await _sandbox_addresses(host, self._port)
            last_error = None
            for address in addresses:
                try:
                    async with asyncio.timeout(8):
                        _, protocol = await asyncio.get_running_loop().create_connection(
                            self._protocol_factory,
                            address,
                            self._port,
                            ssl=self._ssl,
                            # Dial an IP but authenticate the ORIGINAL hostname.
                            server_hostname=(
                                self._config.ssl_target_name_override or host
                                if self._ssl is not None
                                else None
                            ),
                        )
                    return protocol
                except (OSError, TimeoutError) as exc:
                    last_error = exc
            raise ModalConnectionError("Cannot connect to the Modal sandbox worker") from last_error

    original_http_client = http_utils._http_client_with_tls
    original_router_connect = router._connect_channel

    @wraps(original_http_client)
    def http_client_with_system_trust(timeout):
        session = original_http_client(timeout)
        # Preserve the SDK's HTTP/SOCKS proxy connector and bundled public roots,
        # adding host roots for image/blob uploads and downloads as well.
        session.connector._ssl.load_default_certs()
        return session

    @wraps(original_router_connect)
    async def connect_router(channel):
        try:
            async with asyncio.timeout(ROUTER_CONNECT_TIMEOUT):
                await original_router_connect(channel)
        except TimeoutError as exc:
            raise ModalConnectionError(
                f"Modal sandbox connection timed out after {ROUTER_CONNECT_TIMEOUT:g}s. "
                "Check DNS, VPN/proxy access to *.modal.host and the host CA trust. "
                "The sandbox ID is retained; retry the operation when access is restored."
            ) from exc

    grpc_utils.ModalChannel = SystemTrustChannel
    # The router imports the class by value, rather than looking it up at runtime.
    router.ModalChannel = SystemTrustChannel
    router._connect_channel = connect_router
    http_utils._http_client_with_tls = http_client_with_system_trust


async def _connect():
    configure_transport()
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            client = await _Client.from_env()
            # from_env opens a channel but does not validate credentials in 1.6.3.
            await client.hello()
    except TimeoutError as exc:
        raise ModalConnectionError(
            f"Modal connection/authentication check timed out after {CONNECT_TIMEOUT:g}s. "
            "Check VPN/proxy access to Modal and the host CA trust "
            "(SSL_CERT_FILE / SSL_CERT_DIR), then retry. "
            "No box operation was started."
        ) from exc
    except (ssl.SSLError, ConnectionError, OSError) as exc:
        raise ModalConnectionError(
            "Cannot establish a verified connection to Modal. "
            "Check VPN/proxy access and the host CA trust (SSL_CERT_FILE / SSL_CERT_DIR). "
            "No box operation was started."
        ) from exc


# Use the SDK's own loop so lifecycle calls reuse the checked client/channels.
connect = synchronizer.create_blocking(_connect)
