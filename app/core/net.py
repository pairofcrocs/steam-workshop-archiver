"""HTTP session and guarded downloads from Steam hosts."""

import ipaddress
import os
import socket
import tempfile
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Hosts we're willing to fetch user-referenced content (images, files) from.
STEAM_CONTENT_HOST_SUFFIXES = (
    ".steamusercontent.com",
    ".steamstatic.com",
    ".akamaihd.net",
    ".steamcontent.com",
    ".steampowered.com",
    ".steamcommunity.com",
)
STEAM_CONTENT_HOSTS = {"steamusercontent.com", "steamcommunity.com", "steamstatic.com"}

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


SESSION = make_session()


def is_steam_content_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if parsed.port not in (None, 80, 443):
        return False
    return host in STEAM_CONTENT_HOSTS or host.endswith(STEAM_CONTENT_HOST_SUFFIXES)


def is_public_url(url: str) -> bool:
    """True for http(s) URLs on standard ports whose host resolves only to public IPs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.port not in (None, 80, 443):
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")


def _looks_like_image(path: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(12)
    return head.startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


class DownloadError(Exception):
    pass


def safe_download(
    url: str,
    dest: str,
    *,
    max_bytes: int | None = MAX_IMAGE_BYTES,
    timeout: float = 30,
    session: requests.Session | None = None,
    cancel_check=None,
    expect_image: bool = False,
    allow_public: bool = False,
) -> int:
    """Download *url* to *dest* atomically. Only Steam hosts are allowed (or, with
    *allow_public*, any host resolving to public IPs), checked on every redirect
    hop. Returns bytes written. Raises DownloadError."""
    s = session or SESSION
    for _ in range(5):
        allowed = is_steam_content_url(url) or (allow_public and is_public_url(url))
        if not allowed:
            raise DownloadError(f"refusing URL: {url}")
        try:
            r = s.get(url, stream=True, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise DownloadError(str(exc)) from exc
        if r.is_redirect:
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        break
    else:
        raise DownloadError("too many redirects")

    try:
        return _stream_to_file(r, dest, max_bytes, cancel_check, expect_image)
    except DownloadError:
        raise
    except (requests.RequestException, ValueError, OSError) as exc:
        raise DownloadError(str(exc) or exc.__class__.__name__) from exc


def _stream_to_file(r, dest: str, max_bytes: int | None, cancel_check, expect_image: bool) -> int:
    with r:
        if r.status_code != 200:
            raise DownloadError(f"HTTP {r.status_code}")
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        # Steam's UGC CDN often labels images application/octet-stream; magic bytes decide.
        if expect_image and not (ctype.startswith("image/") or ctype in ("application/octet-stream", "")):
            raise DownloadError(f"unexpected content type {ctype!r}")
        length = int(r.headers.get("Content-Length") or 0)
        if max_bytes and length > max_bytes:
            raise DownloadError(f"too large ({length} bytes)")

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
        written = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancel_check and cancel_check():
                        raise DownloadError("cancelled")
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise DownloadError(f"too large (>{max_bytes} bytes)")
                    f.write(chunk)
            if expect_image and not _looks_like_image(tmp):
                raise DownloadError("not an image")
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return written
