"""Web app side of the Hated TV server, used by guide.py.

static(handler, path)  serves web/ under /app/, plus fonts, logo and intro video from app/ (with byte ranges)
relay(handler, qs)     /p?u=<url>[&ua=][&ref=]: fetches a public stream for browsers that cannot load it directly,
                       rewriting playlist links so every follow-up request comes back through the relay."""
import http.client, ipaddress, mimetypes, re, socket, ssl, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
EXTRA = {"intro.mp4": HERE / "app" / "intro" / "intro.mp4", "logo.png": HERE / "app" / "images" / "logo.png"}
FONTS = HERE / "app" / "fonts"
TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8", ".webmanifest": "application/manifest+json",
         ".ttf": "font/ttf", ".png": "image/png", ".mp4": "video/mp4", ".svg": "image/svg+xml"}


def resolve(rel):
    rel = rel.strip("/") or "index.html"
    if rel in EXTRA: return EXTRA[rel]
    if rel.startswith("fonts/"): base, rel = FONTS, rel[len("fonts/"):]
    else: base = WEB
    p = (base / rel).resolve()
    return p if p.is_file() and base.resolve() in p.parents else None


def static(h, rel):
    p = resolve(rel)
    if not p:
        h.send_response(404); h.send_header("Content-Length", "0"); h.end_headers(); return
    data = p.read_bytes()
    ctype = TYPES.get(p.suffix, mimetypes.guess_type(p.name)[0] or "application/octet-stream")
    rng = re.match(r"bytes=(\d*)-(\d*)", h.headers.get("Range", ""))
    if rng:
        start = int(rng.group(1) or 0)
        end = int(rng.group(2)) if rng.group(2) else len(data) - 1
        end = min(end, len(data) - 1)
        part = data[start:end + 1]
        h.send_response(206); h.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
    else:
        part = data
        h.send_response(200)
    h.send_header("Content-Type", ctype); h.send_header("Accept-Ranges", "bytes"); h.send_header("Content-Length", str(len(part)))
    h.send_header("Cache-Control", "no-cache" if p.suffix in (".html", ".js", ".css") else "max-age=86400")
    h.end_headers(); h.wfile.write(part)


NAT64 = ipaddress.ip_network("64:ff9b::/96")


def blocked_ip(ip):
    """Anything that is not a plain public internet address is blocked, including IPv6 forms that embed IPv4."""
    if ip.version == 6:
        if ip in NAT64: return True
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None: return blocked_ip(inner)
    return not ip.is_global or ip.is_multicast


def public_host(url):
    """Only relay to public internet addresses, never to private, loopback, link-local or shared (carrier-grade NAT) ranges.
    Every address the name resolves to must be public."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname: return False
    try:
        ips = {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(u.hostname, None)}
    except Exception:
        return False
    return bool(ips) and not any(blocked_ip(ip) for ip in ips)


def check_peer(sock):
    """Called right after the TCP connection opens, before TLS or any request bytes: the address actually
    reached must be public, so a name that changes between lookups cannot point the relay at a private network."""
    try:
        peer = ipaddress.ip_address(sock.getpeername()[0])
    except Exception:
        peer = None
    if peer is None or blocked_ip(peer):
        sock.close()
        raise PermissionError("relay target is not a public address")


class SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        check_peer(self.sock)


class SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        check_peer(self.sock)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req): return self.do_open(SafeHTTPConnection, req)


class SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req): return self.do_open(SafeHTTPSConnection, req, context=ssl.create_default_context())


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only when its target passes public_host too."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_host(newurl): return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(url, hdr, timeout):
    opener = urllib.request.build_opener(SafeHTTPHandler, SafeHTTPSHandler, CheckedRedirect)
    return opener.open(urllib.request.Request(url, headers=hdr), timeout=timeout)


MEDIA = re.compile(r"^(video/|audio/|application/(vnd\.apple\.mpegurl|x-mpegurl|octet-stream|mp4|dash\+xml)|image/)", re.I)


def relay_link(url, ua, ref):
    q = {"u": url}
    if ua: q["ua"] = ua
    if ref: q["ref"] = ref
    return "/p?" + urllib.parse.urlencode(q)


def rewrite(text, base, ua, ref):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(relay_link(urllib.parse.urljoin(base, s), ua, ref))
        elif 'URI="' in s:
            out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + relay_link(urllib.parse.urljoin(base, m.group(1)), ua, ref) + '"', line))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def relay(h, qs):
    url = (qs.get("u") or [""])[0]
    ua = (qs.get("ua") or [""])[0]
    ref = (qs.get("ref") or [""])[0]
    if not public_host(url):
        h.send_response(403); h.send_header("Content-Length", "0"); h.end_headers(); return
    hdr = {"User-Agent": ua or "Mozilla/5.0"}
    if ref: hdr["Referer"] = ref
    r = open_public(url, hdr, 20)
    ctype = r.headers.get("Content-Type", "")
    final = r.geturl()
    if "mpegurl" in ctype.lower() or final.split("?")[0].lower().endswith(".m3u8"):
        body = rewrite(r.read().decode("utf-8", errors="replace"), final, ua, ref).encode("utf-8")
        h.send_response(200); h.send_header("Content-Type", "application/vnd.apple.mpegurl")
        h.send_header("Content-Length", str(len(body))); h.end_headers()
        h.wfile.write(body); return
    safe = ctype if MEDIA.match(ctype or "") else "application/octet-stream"
    h.send_response(200); h.send_header("Content-Type", safe)
    h.send_header("X-Content-Type-Options", "nosniff"); h.send_header("Content-Security-Policy", "sandbox")
    length = r.headers.get("Content-Length")
    if length: h.send_header("Content-Length", length)
    h.end_headers()
    while True:
        chunk = r.read(65536)
        if not chunk: break
        h.wfile.write(chunk)
