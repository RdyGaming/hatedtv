"""Channel hub for Hated TV (United States only), used by guide.py.

serve(path) answers /iptv/<path> like iptv-org would, limited to US channels:
  countries/us.m3u and index.m3u   every US channel (iptv-org US list plus Free-TV US)
  categories/<x>.m3u               that genre, US channels only
  languages/<x>.m3u                that language, US channels only
  subdivisions/us-<st>.m3u         that state, as published
  services/<slug>.m3u              US channels hosted by one free service
Every list drops duplicates, hides channels the health checker found dead or blocked,
and tags channels that have guide data with hated-guide="<key>".
Background loops: health checks every CHECK_EVERY seconds, extra guide data every GUIDE_EVERY seconds."""
import gzip, io, json, re, threading, time, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IPTV = "https://iptv-org.github.io/iptv/"
FREETV = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
EPGS = [("sam", "https://i.mjh.nz/SamsungTVPlus/us.xml.gz"), ("plex", "https://i.mjh.nz/Plex/us.xml.gz"), ("roku", "https://i.mjh.nz/Roku/all.xml.gz")]
SERVICES = [("pluto", "Pluto TV", r"pluto|jmp2\.uk/plu-"), ("samsung", "Samsung TV Plus", r"samsung|jmp2\.uk/sam-"),
            ("plex", "Plex", r"plex"), ("roku", "The Roku Channel", r"roku"), ("xumo", "Xumo", r"xumo"),
            ("tubi", "Tubi", r"tubi"), ("stirr", "Stirr", r"stirr|sinclair"), ("localnow", "Local Now", r"localnow")]
CHECK_EVERY, GUIDE_EVERY, TTL = 12 * 3600, 3 * 3600, 1800
HEALTH_FILE = Path(__file__).with_name("health.json")
UA = "Mozilla/5.0"
PLUTO = re.compile(r"pluto|jmp2\.uk/plu-")

cache = {}
us = {"base": [], "extra": [], "urls": set(), "updated": 0}
health = {}
hstat = {"running": False, "checked": 0, "dead": 0, "total": 0, "finished": 0}
epg = {"names": {}, "progs": {}, "updated": 0, "matched": 0, "error": ""}
lock = threading.Lock()


def get_bytes(url, timeout=60):
    for wait in (3, 10, 0):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or not wait: raise
        except urllib.error.URLError:
            if not wait: raise
        time.sleep(wait)


def get_text(url, ttl=TTL):
    hit = cache.get(url)
    if hit and time.time() - hit[0] < ttl: return hit[1]
    try:
        text = get_bytes(url).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 404: raise
        text = ""
    cache[url] = (time.time(), text)
    return text


CIRCLED = re.compile("[①-⓿]")


def normname(t):
    t = CIRCLED.sub("", t)
    t = re.sub(r"\(.*?\)|\[.*?\]", "", t).lower()
    t = re.sub(r"\b(hd|fhd|uhd|sd|4k)\b", "", t)
    return re.sub(r"[^a-z0-9]", "", t)


def title_of(extinf):
    m = re.search(r',([^"]*)$', extinf)
    return m.group(1).strip() if m else ""


def blocks(text):
    """[(lines, url)]: the #EXTINF line plus any #EXTVLCOPT lines, then the stream link."""
    out, cur = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line: continue
        if line.startswith("#EXTINF"): cur = [line]
        elif line.startswith("#EXTVLCOPT") and cur: cur.append(line)
        elif not line.startswith("#") and cur:
            out.append((cur, line)); cur = []
    return out


def opts(lines):
    ua = ref = ""
    for l in lines[1:]:
        if "http-user-agent" in l: ua = l.split("=", 1)[1]
        if "http-referrer" in l: ref = l.split("=", 1)[1]
    return ua, ref


def load_us():
    """The US universe: iptv-org's US list, then Free-TV's US channels that iptv-org lacks."""
    base = blocks(get_text(IPTV + "countries/us.m3u", 6 * 3600))
    have_url = {u for _, u in base}
    have_name = {normname(title_of(l[0])) for l, _ in base}
    extra = []
    for lines, url in blocks(get_text(FREETV, 6 * 3600)):
        head = lines[0]
        group = (re.search(r'group-title="([^"]*)"', head) or [None, ""])[1].lower()
        ccs = (re.search(r'tvg-country="([^"]*)"', head) or [None, ""])[1].upper()
        if group.startswith("vod") or not ("US" in re.split(r"[;,\s]+", ccs) or group == "usa"): continue
        title = CIRCLED.sub("", title_of(head)).strip()
        n = normname(title)
        if url in have_url or (n and n in have_name): continue
        have_url.add(url); have_name.add(n)
        extra.append(([head[: head.rfind(",") + 1] + title] + lines[1:], url))
    us.update(base=base, extra=extra, urls=have_url, updated=int(time.time()))


def ensure_loaded():
    if not us["base"] or time.time() - us["updated"] > 6 * 3600: load_us()


def guide_key(url, title):
    if PLUTO.search(url): return ""
    return epg["names"].get(normname(title), "")


def entries_for(path):
    if path in ("countries/us.m3u", "index.m3u"): return us["base"] + us["extra"]
    m = re.match(r"services/([a-z]+)\.m3u$", path)
    if m:
        rx = next((s[2] for s in SERVICES if s[0] == m.group(1)), None)
        return [b for b in us["base"] + us["extra"] if rx and re.search(rx, b[1], re.I)]
    if path.startswith("subdivisions/us-"): return blocks(get_text(IPTV + path))
    if re.match(r"(categories|languages)/[a-z0-9_-]+\.m3u$", path):
        found = [b for b in blocks(get_text(IPTV + path)) if b[1] in us["urls"]]
        if path == "languages/eng.m3u": found += us["extra"]
        return found
    return []


def serve(path, strict=False):
    """strict: browser lists on the public site, which has no relay, so skip streams a browser cannot load directly."""
    ensure_loaded()
    seen, out, hidden = set(), [], 0
    for lines, url in entries_for(path):
        if url in seen: continue
        seen.add(url)
        h = health.get(url)
        if h and not h[0]:
            hidden += 1
            continue
        head = lines[0]
        if strict and (len(lines) > 1 or (h and len(h) > 2 and not h[2]) or url.startswith("http:")):
            head = head.replace("#EXTINF:-1", '#EXTINF:-1 hated-web="0"', 1)
        key = guide_key(url, title_of(head))
        if key: head = head.replace("#EXTINF:-1", f'#EXTINF:-1 hated-guide="{key}"', 1)
        out.append("\n".join([head] + lines[1:] + [url]))
    return "#EXTM3U\n" + "\n".join(out) + "\n", len(out), hidden


# ---------- health checks ----------

ORIGIN = "https://hatedtv.example"


def cors_ok(resp):
    """A browser accepts the response only with exactly one Access-Control-Allow-Origin of * or its own origin."""
    vals = resp.headers.get_all("Access-Control-Allow-Origin") or []
    return len(vals) == 1 and vals[0].strip() in ("*", ORIGIN)


def check(url, ua, ref):
    """Returns (plays, browser_ok): plays = playlist, variant and first video piece all load;
    browser_ok = every one of those answers with valid cross-origin permission for a web page."""
    h = {"User-Agent": ua or UA, "Origin": ORIGIN}
    if ref: h["Referer"] = ref
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=8)
        cors = cors_ok(r)
        body = r.read(65536).decode("utf-8", errors="replace")
        if ".m3u8" in url.lower() or body.lstrip().startswith("#EXTM3U"):
            if not body.lstrip().startswith("#EXTM3U"): return False, False
            media, base = body, r.geturl()
            if "#EXT-X-STREAM-INF" in body:
                var = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), "")
                if not var: return False, False
                v = urllib.request.urlopen(urllib.request.Request(urllib.parse.urljoin(base, var), headers=h), timeout=8)
                cors = cors and cors_ok(v)
                media, base = v.read(65536).decode("utf-8", errors="replace"), v.geturl()
                if not media.lstrip().startswith("#EXTM3U"): return False, False
            if "takedown" in media.lower(): return False, False
            seg = next((l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")), "")
            if seg:
                sh = dict(h, Range="bytes=0-2047")
                s = urllib.request.urlopen(urllib.request.Request(urllib.parse.urljoin(base, seg), headers=sh), timeout=8)
                cors = cors and cors_ok(s)
                if not s.read(2048): return False, False
        return True, cors
    except Exception:
        return False, False


def save_health():
    with lock:
        HEALTH_FILE.write_text(json.dumps(health, separators=(",", ":")), encoding="utf-8")


def run_checks():
    load_us()
    todo = {u: opts(l) for l, u in us["base"] + us["extra"]}
    hstat.update(running=True, checked=0, total=len(todo))
    with ThreadPoolExecutor(32) as ex:
        futs = {ex.submit(check, u, *o): u for u, o in todo.items()}
        for n, f in enumerate(futs, 1):
            plays, browser_ok = f.result()
            health[futs[f]] = [plays, int(time.time()), browser_ok]
            hstat["checked"] = n
            if n % 500 == 0: save_health()
    for u in [u for u in health if u not in todo]: health.pop(u, None)
    save_health()
    hstat.update(running=False, dead=sum(1 for v in health.values() if not v[0]), finished=int(time.time()))
    print(time.strftime("%H:%M"), f"health: {hstat['dead']} of {len(health)} US streams dead or blocked", flush=True)


def health_loop():
    if HEALTH_FILE.exists():
        try:
            health.update(json.loads(HEALTH_FILE.read_text(encoding="utf-8")))
            hstat.update(dead=sum(1 for v in health.values() if not v[0]), finished=max((v[1] for v in health.values()), default=0))
        except Exception:
            pass
    while True:
        if time.time() - hstat["finished"] >= CHECK_EVERY:
            try: run_checks()
            except Exception as ex: hstat["running"] = False; print("health check failed:", ex, flush=True)
        time.sleep(600)


# ---------- extra guide data ----------

def xmltv_time(s):
    return int(datetime.datetime.strptime(s.strip()[:20], "%Y%m%d%H%M%S %z").timestamp())


def load_epg(hours=8):
    now = time.time()
    lo, hi = now - 3600, now + hours * 3600
    names, progs = {}, {}
    for src, url in EPGS:
        raw = gzip.decompress(get_bytes(url, timeout=120))
        for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
            if el.tag == "channel":
                n = normname(el.findtext("display-name") or "")
                if n and n not in names: names[n] = f"{src}:{el.get('id')}"
                el.clear()
            elif el.tag == "programme":
                try:
                    s, e = xmltv_time(el.get("start")), xmltv_time(el.get("stop"))
                except Exception:
                    el.clear(); continue
                if e > lo and s < hi:
                    title = el.findtext("title") or ""
                    sub = el.findtext("sub-title") or ""
                    if sub and sub != title: title = f"{title}: {sub}"
                    progs.setdefault(f"{src}:{el.get('channel')}", []).append([s, e - s, title])
                el.clear()
    for v in progs.values(): v.sort()
    ensure_loaded()
    wanted = set()
    for lines, url in us["base"] + us["extra"]:
        if PLUTO.search(url): continue
        k = names.get(normname(title_of(lines[0])))
        if k and k in progs: wanted.add(k)
    epg["names"] = {n: k for n, k in names.items() if k in wanted}
    epg["progs"] = {k: progs[k] for k in wanted}
    epg.update(updated=int(time.time()), matched=len(wanted), error="")
    print(time.strftime("%H:%M"), f"extra guide: {len(wanted)} US channels matched", flush=True)


def guide_loop():
    while True:
        try: load_epg()
        except Exception as ex: epg["error"] = str(ex); print("extra guide failed:", ex, flush=True)
        time.sleep(GUIDE_EVERY)


def start():
    threading.Thread(target=health_loop, daemon=True).start()
    threading.Thread(target=guide_loop, daemon=True).start()


def status():
    return {"us_channels": len(us["base"]) + len(us["extra"]), "freetv_added": len(us["extra"]),
            "health_running": hstat["running"], "health_checked": hstat["checked"], "health_total": hstat["total"],
            "health_dead": hstat["dead"], "health_finished": hstat["finished"],
            "extra_guide_channels": epg["matched"], "extra_guide_error": epg["error"]}


if __name__ == "__main__":
    ensure_loaded()
    print("US universe:", len(us["base"]), "iptv-org +", len(us["extra"]), "Free-TV")
    for p in ["countries/us.m3u", "categories/news.m3u", "languages/eng.m3u", "subdivisions/us-mn.m3u", "services/samsung.m3u", "services/pluto.m3u"]:
        _, n, hidden = serve(p)
        print(f"{p:26} {n:5} channels, {hidden} hidden")
