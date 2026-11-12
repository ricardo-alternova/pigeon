"""Library module for bounded, address-pinned HTTPS unsubscribe transport.

Run through scripts/execute.py unsubscribe; this module has no CLI.
"""
import http.client
from html.parser import HTMLParser
import ipaddress
import re
import signal
import socket
import ssl
from urllib.parse import urlsplit, urljoin

LIMIT = 16384
TIMEOUT = 5
DEADLINE = 20


class BoundedResponse(http.client.HTTPResponse):
    def begin(self):
        original = self.fp
        class Headers:
            used = 0
            def readline(self, size=-1):
                line = original.readline(min(size, LIMIT + 1 - self.used) if size >= 0 else LIMIT + 1 - self.used)
                self.used += len(line)
                if self.used > LIMIT:
                    raise http.client.HTTPException("response headers exceed cap")
                return line
        self.fp = Headers()
        try:
            super().begin()
        finally:
            if self.fp is not None:
                self.fp = original



def public(address):
    ip = ipaddress.ip_address(address)
    ip = getattr(ip, 'ipv4_mapped', None) or ip
    if ip.version == 6 and (ip not in ipaddress.ip_network('2000::/3')
                            or ip in ipaddress.ip_network('2001::/23')
                            or ip in ipaddress.ip_network('2002::/16')
                            or ip in ipaddress.ip_network('3fff::/20')):
        return False
    if ip.version == 4 and any(ip in ipaddress.ip_network((base, bits)) for base, bits in
                               ((0xc0000000, 24), (0xc0586300, 24))):
        return False
    # is_global also excludes shared address space; multicast needs its own guard.
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class PasswordForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.password = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'input' and any(k.lower() == 'type' and (v or '').lower() == 'password' for k, v in attrs):
            self.password = True


def exchange(host, address, method, target):
    connection = http.client.HTTPSConnection(host, timeout=TIMEOUT)
    connection.response_class = BoundedResponse
    raw = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw.settimeout(TIMEOUT)
        raw.connect((address, 443))
        connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        body = b'List-Unsubscribe=One-Click' if method == 'POST' else None
        headers = {'User-Agent': 'Mail-Unsubscribe/1.0', 'Accept': 'text/html'}
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read(LIMIT + 1)
    finally:
        connection.close()
        raw.close()


def _request(url, one_click=False, *, resolver=socket.getaddrinfo, transport=exchange):
    method = 'POST' if one_click else 'GET'
    try:
        for hop in range(4):
            parsed = urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme != 'https' or not host or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 443)
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                    or '\\' in url or '%' in host):
                return 'failed'
            answers = resolver(host, 443, type=socket.SOCK_STREAM)
            addresses = [a[4][0] for a in answers]
            if not addresses or not all(public(a) for a in addresses):
                return 'failed'
            status, headers, body = transport(host, addresses[0], method, (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''))
            parser = PasswordForm()
            parser.feed(body[:LIMIT].decode('utf-8', errors='replace'))
            if status in (401, 407) or 'www-authenticate' in headers or parser.password:
                return 'failed'
            if status in (301, 302, 303, 307, 308):
                if hop == 3 or not headers.get('location'):
                    return 'failed'
                url = urljoin(url, headers['location'])
                # Browser semantics: 301/302/303 change POST to GET; 307/308 preserve RFC 8058 body.
                if status in (301, 302, 303):
                    method = 'GET'
                continue
            return 'executed' if 200 <= status < 300 else 'failed'
    except (ValueError, OSError, http.client.HTTPException):
        return 'failed'


def request(url, one_click=False, *, resolver=socket.getaddrinfo, transport=exchange):
    """One POSIX wall-clock budget, including DNS and every redirect."""
    def expired(signum, frame):
        raise TimeoutError('unsubscribe deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, DEADLINE)
    try:
        return _request(url, one_click, resolver=resolver, transport=transport)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)
