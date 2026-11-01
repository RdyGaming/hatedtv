"""Hated TV server (run with pythonw, desktop shortcut "Hated TV Guide"). Port 8099.
  /guide.json              Pluto live schedules: { channel_id: [[start_epoch, duration_s, title], ...] }
  /vod/categories          Pluto on-demand shelves: [{id, name, count}]
  /vod/category/<id>       items of one shelf: [{id, name, type, poster, image, summary, genre, rating, minutes, hls}]
  /vod/series/<id>         episodes of a series, same item shape plus season and number
  /vod/search?q=text       items whose name contains text
  /vod/open?id=<id>&type=movie|episode|series   tells the TV to open that title in the official Pluto TV channel
  /iptv/<path>             US channel lists from the channel hub (hub.py)
  /status
Pluto endpoints are unofficial and may change without notice."""
import json, threading, time, datetime, uuid, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import hub
import webserve


import os
PUBLIC = os.environ.get("HATEDTV_PUBLIC") == "1"          # cloud mode: no relay, no Play on TV, no home name server
PREFIX = os.environ.get("HATEDTV_PREFIX", "").rstrip("/")  # e.g. /hatedtv when served at hatedgaming.net/hatedtv
HOST = os.environ.get("HATEDTV_HOST", "")
PORT = int(os.environ.get("HATEDTV_PORT", "8099"))
HOURS, REFRESH = 8, 1200
TV = os.environ.get("HATEDTV_TV", "")  # Roku TV address for Play on TV, home mode only (set by the control panel)
PLUTO_APP = "74519"
UTC = datetime.timezone.utc
UA = {"User-Agent": "Mozilla/5.0"}
state = {"guide": {}, "updated": 0, "error": ""}
pluto = {"token": "", "params": "", "servers": {}, "exp": 0, "client": str(uuid.uuid4()), "cats": [], "catItems": {}, "items": {}, "updated": 0, "error": ""}
lock = threading.Lock()

def get_json(url, headers=None, timeout=60):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=timeout))

def epoch(s):
    return int(datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp())

# ---------- live guide ----------

def fetch_pluto_guide():
    now = datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    start, stop = (now - datetime.timedelta(hours=1)).strftime(fmt), (now + datetime.timedelta(hours=HOURS)).strftime(fmt)
    out = {}
    for c in get_json(f"https://api.pluto.tv/v2/channels?start={start}&stop={stop}"):
        progs = []
        for t in c.get("timelines", []):
            title = t.get("title") or ""
            ep = (t.get("episode") or {}).get("name") or ""
            if ep and ep != title: title = f"{title}: {ep}"
            s, e = epoch(t["start"]), epoch(t["stop"])
            if e > s: progs.append([s, e - s, title])
        if progs: out[c["_id"]] = progs
    return out

# ---------- on demand ----------

def boot():
    if pluto["token"] and time.time() < pluto["exp"]: return
    q = {"appName": "web", "appVersion": "9.0.0", "deviceVersion": "120.0.0", "deviceModel": "web", "deviceMake": "chrome", "deviceType": "web",
         "clientID": pluto["client"], "clientModelNumber": "1.0.0", "serverSideAds": "false", "drmCapabilities": "widevine:L3"}
    b = get_json("https://boot.pluto.tv/v4/start?" + urllib.parse.urlencode(q))
    pluto["token"], pluto["params"], pluto["servers"] = b["sessionToken"], b["stitcherParams"], b["servers"]
    pluto["exp"] = time.time() + min(int(b.get("refreshInSec", 3600)), 3600) - 60

def auth():
    boot()
    return {"Authorization": "Bearer " + pluto["token"]}

def compact(i):
    poster, image = "", ""
    for c in i.get("covers") or []:
        if c.get("aspectRatio") == "347:500": poster = c.get("url", "")
    fi = i.get("featuredImage") or {}
    image = fi.get("path", "")
    if not poster: poster = image
    hls = ""
    for p in (i.get("stitched") or {}).get("paths") or []:
        if p.get("type") == "hls": hls = p.get("path", "")
    ms = i.get("originalContentDuration") or i.get("duration") or 0
    end = (i.get("avail") or {}).get("endDate") or ""
    ends = int(datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()) if end else 0
    return {"ends": ends, "id": i.get("_id", ""), "name": i.get("name", ""), "type": i.get("type", ""), "poster": poster, "image": image,
            "summary": (i.get("summary") or i.get("description") or "")[:400], "genre": i.get("genre", ""), "rating": i.get("rating", ""),
            "minutes": int(ms / 60000), "hls": hls, "season": i.get("season", 0), "number": i.get("number", 0)}

def playable(item):
    """Pluto's Roku app drops titles about a day before their end date, so links to them open nothing."""
    return not item["ends"] or item["ends"] - time.time() > 86400


def fetch_catalog():
    cats, catItems, items = [], {}, {}
    page, pages = 1, 1
    while page <= pages and page <= 6:
        data = get_json(pluto["servers"]["vod"] + f"/v4/vod/categories?includeItems=true&offset=0&page={page}", auth())
        pages = int(data.get("totalPages", 1))
        for c in data.get("categories", []):
            lst = [x for x in (compact(i) for i in c.get("items", [])) if playable(x)]
            if not lst: continue
            cats.append({"id": c["_id"], "name": c["name"], "count": len(lst)})
            catItems[c["_id"]] = lst
            for it in lst: items[it["id"]] = it
        page += 1
    with lock:
        pluto["cats"], pluto["catItems"], pluto["items"], pluto["updated"] = cats, catItems, items, int(time.time())

def series_episodes(sid):
    data = get_json(pluto["servers"]["vod"] + f"/v4/vod/series/{sid}/seasons?includeItems=true&offset=0&page=1", auth())
    out = []
    for s in data.get("seasons", []):
        for e in s.get("episodes", []):
            it = compact(e)
            if not playable(it): continue
            if not it["image"]: it["image"] = (data.get("featuredImage") or {}).get("path", "")
            if not it["poster"]: it["poster"] = it["image"]
            out.append(it)
    out.sort(key=lambda e: (e["season"], e["number"]))
    return out

def open_in_pluto(cid, mtype):
    q = urllib.parse.urlencode({"contentId": cid, "mediaType": mtype or "movie"})
    r = urllib.request.urlopen(urllib.request.Request(f"{TV}/launch/{PLUTO_APP}?{q}", data=b"", method="POST"), timeout=10)
    return {"status": r.status, "app": PLUTO_APP, "contentId": cid, "mediaType": mtype}

# ---------- loops and http ----------

def refresher():
    while True:
        ok = True
        try:
            g = fetch_pluto_guide()
            state["guide"] = g
            state["updated"] = int(time.time()); state["error"] = ""
            print(time.strftime("%H:%M"), "guide refreshed:", len(g), "channels", flush=True)
        except Exception as ex:
            state["error"] = str(ex); ok = False; print("guide refresh failed:", ex, flush=True)
        try:
            boot(); fetch_catalog(); pluto["error"] = ""
            print(time.strftime("%H:%M"), "on demand refreshed:", len(pluto["cats"]), "shelves,", len(pluto["items"]), "titles", flush=True)
        except Exception as ex:
            pluto["error"] = str(ex); ok = False; print("on demand refresh failed:", ex, flush=True)
        time.sleep(REFRESH if ok else 60)

def J(o): return json.dumps(o, separators=(",", ":")).encode()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); qs = urllib.parse.parse_qs(u.query); p = u.path
        try:
            if PREFIX and p.startswith(PREFIX + "/"): p = p[len(PREFIX):]
            if p in ("/", "/app", PREFIX):
                self.send_response(302); self.send_header("Location", PREFIX + "/app/"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if p.startswith("/app/"): return webserve.static(self, p[len("/app/"):])
            if p == "/p":
                if PUBLIC:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                return webserve.relay(self, qs)
        except Exception:
            try: self.send_response(502); self.send_header("Content-Length", "0"); self.end_headers()
            except Exception: pass
            return
        code, body, ctype = 200, b"{}", "application/json"
        try:
            if p.startswith("/guide"): body = J({**state["guide"], **hub.epg["progs"]})
            elif p == "/config": body = J({"public": PUBLIC, "relay": not PUBLIC, "playOnTv": not PUBLIC})
            elif p.startswith("/iptv/") or p.startswith("/iptv-tv/"):
                browser = p.startswith("/iptv/")
                text, _, _ = hub.serve(p.split("/", 2)[2], strict=PUBLIC and browser)
                body, ctype = text.encode("utf-8"), "audio/x-mpegurl"
            elif p == "/vod/categories": body = J(pluto["cats"])
            elif p.startswith("/vod/category/"): body = J(pluto["catItems"].get(p.rsplit("/", 1)[1], []))
            elif p.startswith("/vod/series/"): body = J(series_episodes(p.rsplit("/", 1)[1]))
            elif p == "/vod/index.json": body = J([[i["id"], i["name"], i["type"], i["poster"] or i["image"]] for i in pluto["items"].values()])
            elif p == "/vod/search":
                q = (qs.get("q") or [""])[0].strip().lower()
                body = J([i for i in pluto["items"].values() if q and q in i["name"].lower()][:120])
            elif p == "/vod/open":
                if PUBLIC: code, body = 404, J({"error": "Play on TV is only available at home"})
                else: body = J(open_in_pluto((qs.get("id") or [""])[0], (qs.get("type") or ["movie"])[0]))
            else: body = J({"guide_updated": state["updated"], "guide_error": state["error"], "vod_updated": pluto["updated"], "vod_error": pluto["error"], "shelves": len(pluto["cats"]), "titles": len(pluto["items"]), **hub.status()})
        except Exception as ex:
            code, body, ctype = 500, J({"error": str(ex)}), "application/json"
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

if __name__ == "__main__":
    import sys
    from pathlib import Path
    if sys.stdout is None:
        log = Path(__file__).with_name("server.log")
        mode = "w" if log.exists() and log.stat().st_size > 1_000_000 else "a"
        sys.stdout = sys.stderr = open(log, mode, buffering=1, encoding="utf-8")
        print(time.strftime("%Y-%m-%d %H:%M"), "server started", flush=True)
    threading.Thread(target=refresher, daemon=True).start()
    hub.start()
    if not PUBLIC:
        import dns
        dns.start()
        try:
            web80 = ThreadingHTTPServer(("", 80), H)
            threading.Thread(target=web80.serve_forever, daemon=True).start()
            print("web app also on port 80", flush=True)
        except OSError as ex:
            print("port 80 unavailable:", ex, flush=True)
    print("Hated TV server on port", PORT, "(public mode)" if PUBLIC else "", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
