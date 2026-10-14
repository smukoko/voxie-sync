"""The gateway's address, decided in one place.

~/.librasync.json holds GATEWAY_IP (any host) and, once the gateway sits
behind TLS, GATEWAY_URL ("https://voxie.your-tailnet.ts.net"). Every client
-- the KOReader plugin and its Progress sync, the catalogue, the watcher's
upload, the health check -- asks gateway_url() instead of building
"http://<ip>:3000" itself, so moving to HTTPS is one setting, not seven.
"""
from urllib.parse import urlparse

GATEWAY_PORT = 3000
ABS_PORT = 13378


def abs_url(config):
    """Audiobookshelf's address from the Mac, or None if no server is set.

    ABS_PUBLIC_URL when the server sits behind TLS (Caddy serves ABS on
    https://<domain>:13378); otherwise plain http on the gateway's host.
    """
    url = (config.get("ABS_PUBLIC_URL") or "").strip().rstrip("/")
    if url:
        return url
    host = (config.get("GATEWAY_IP") or "").strip()
    return f"http://{host}:{ABS_PORT}" if host else None


def gateway_url(config):
    """The base URL clients use, without a trailing slash, or None if unset."""
    url = (config.get("GATEWAY_URL") or "").strip().rstrip("/")
    if url:
        return url
    host = (config.get("GATEWAY_IP") or "").strip()
    return as_url(host) if host else None


def as_url(host_or_url):
    """'91.99.12.34' -> 'http://91.99.12.34:3000'; a full URL is kept as written."""
    text = host_or_url.strip().rstrip("/")
    return text if "://" in text else f"http://{text}:{GATEWAY_PORT}"


def parse_gateway(text):
    """What `voxie init` was given -> (host, url or None).

    A bare IP or hostname keeps the old http://<host>:3000. A full URL is kept
    whole, because behind a TLS proxy (Tailscale Funnel, Caddy) the port and
    the scheme are the proxy's, not the gateway's.
    """
    text = text.strip()
    if "://" not in text:
        return text.split("/")[0], None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Not a web address: {text!r}")
    return parsed.hostname, text.rstrip("/")
