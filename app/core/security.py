import hashlib
import hmac
import ipaddress
import secrets
import socket
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import HttpUrl, TypeAdapter, ValidationError

MAX_URL_LENGTH = 2048
HostResolver = Callable[[str], Iterable[str]]

_http_url = TypeAdapter(HttpUrl)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_6TO4 = ipaddress.ip_network("2002::/16")


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), api_key.encode(), hashlib.sha256).hexdigest()


def hash_ip(ip: str, pepper: str) -> str:
    """Keyed, irreversible-without-the-pepper digest of a visitor IP (raw IPs are never stored)."""
    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        canonical = str(address)
    except ValueError:
        canonical = ip
    return hmac.new(pepper.encode(), b"click-ip:" + canonical.encode(), hashlib.sha256).hexdigest()


class UnsafeURLError(ValueError):
    """The target URL is rejected by link-safety policy (maps to 422)."""


def normalize_host(host: str) -> str:
    return host.strip("[]").lower().rstrip(".")


def own_hostnames(request_host: str | None, public_base_url: str | None) -> frozenset[str]:
    """Hostnames that identify this service. The configured public URL cannot be spoofed."""
    hosts = set()
    if request_host:
        hosts.add(normalize_host(request_host))
    if public_base_url and (configured := urlsplit(public_base_url).hostname):
        hosts.add(normalize_host(configured))
    return frozenset(hosts)


def load_blocked_domains(path: str | Path) -> frozenset[str]:
    """Read one domain per line; blank lines and `#` comments are ignored."""
    domains = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            domains.add(normalize_host(line))
    return frozenset(domains)


def resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0].split("%", 1)[0] for info in infos})


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    `is_global` is False for loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7),
    link-local (169.254/16, fe80::/10), unspecified, CGNAT and other reserved ranges.
    """
    if isinstance(ip, ipaddress.IPv6Address) and (embedded := _embedded_ipv4(ip)) is not None:
        return is_public_ip(embedded)
    return ip.is_global and not ip.is_multicast


def _is_blocked_domain(host: str, blocked_domains: Collection[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in blocked_domains)


def check_target_url(
    long_url: str,
    *,
    own_hosts: Collection[str],
    blocked_domains: Collection[str],
    resolver: HostResolver = resolve_host,
) -> None:
    """Raise UnsafeURLError unless `long_url` is an acceptable redirect target.

    The host is taken from a WHATWG-normalised parse, so forms such as `2130706433`,
    `0x7f.1` or `[::ffff:127.0.0.1]` are checked as the addresses browsers will use.
    Every address a hostname resolves to must be public (fail closed on DNS errors).
    """
    if len(long_url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"URL is longer than {MAX_URL_LENGTH} characters")
    try:
        parsed = _http_url.validate_python(long_url)
    except ValidationError:
        raise UnsafeURLError("URL must be a valid http or https URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise UnsafeURLError("URL must be a valid http or https URL")

    host = normalize_host(parsed.host)
    if host in own_hosts:
        raise UnsafeURLError("URL must not point back at this service")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURLError("URL targets a local or private address")
    if _is_blocked_domain(host, blocked_domains):
        raise UnsafeURLError("URL domain is blocked")

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a) for a in resolver(host)]
        except (OSError, ValueError):
            raise UnsafeURLError("URL host could not be resolved") from None
    if not addresses:
        raise UnsafeURLError("URL host could not be resolved")
    if not all(is_public_ip(address) for address in addresses):
        raise UnsafeURLError("URL targets a local or private address")
