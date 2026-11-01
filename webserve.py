"""Web app side of the Hated TV server, used by guide.py.

static(handler, path)  serves web/ under /app/, plus the intro video, fonts and logo from the Roku app folder (with byte ranges)
relay(handler, qs)     /p?u=<url>[&ua=][&ref=]: fetches a public stream for browsers that cannot load it directly,
                       rewriting playlist links so every follow-up request comes back through the relay."""
import ipaddress, mimetypes, re, socket, urllib.parse, urllib.request
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


SHARED = ipaddress.ip_network("100.64.0.0/10")   # carrier-grade NAT, also every Tailscale address


def blocked_ip(ip):
    if getattr(ip, "ipv4_mapped", None): ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or (ip.version == 4 and ip in SHARED))


def public_host(url):
    """Only relay to public internet addresses, never to the home network, the tailnet or this PC.
    Every address the name resolves to must be public."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname: return False
    try:
        ips = {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(u.hostname, None)}
    except Exception:
        return False
    return bool(ips) and not any(blocked_ip(ip) for ip in ips)


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only when its target passes public_host too."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_host(newurl): return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(url, hdr, timeout):
    """Opens url after checking it, refusing redirects to private addresses. The connection itself
    re-checks the address it actually connected to, so a name that changes between lookups is refused."""
    opener = urllib.request.build_opener(CheckedRedirect)
    r = opener.open(urllib.request.Request(url, headers=hdr), timeout=timeout)
    try:
        sock = r.fp.raw._sock if hasattr(r.fp, "raw") else None
        peer = sock.getpeername()[0] if sock else None
    except Exception:
        peer = None
    if peer and blocked_ip(ipaddress.ip_address(peer)):
        r.close(); raise PermissionError("relay target resolved to a private address")
    return r


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
    h.send_response(200); h.send_header("Content-Type", ctype or "application/octet-stream")
    length = r.headers.get("Content-Length")
    if length: h.send_header("Content-Length", length)
    h.end_headers()
    while True:
        chunk = r.read(65536)
        if not chunk: break
        h.wfile.write(chunk)
