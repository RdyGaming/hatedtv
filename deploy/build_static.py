"""Builds the whole public Hated TV site as static files in site/ (published at <pages>/hatedtv/).

Run by .github/workflows/build.yml every 2 hours. Uses the same code as the home server (hub.py, guide.py):
  iptv/<list>.m3u      browser lists (channels a desktop browser cannot load are tagged hated-web="0")
  iptv-tv/<list>.m3u   TV app lists
  guide.json           Pluto plus matched guide data, next 8 hours
  vod/categories, vod/category/<id>, vod/index.json   Pluto On Demand catalog
  config, status, health.json, app/   settings, counts, channel test results, web app
The channel test runs when RUN_HEALTH=1; otherwise the last published results (HATEDTV_PUBLISHED/health.json) are reused."""
import json, os, shutil, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HATEDTV_PUBLIC", "1")
import hub    # noqa: E402
import guide  # noqa: E402

OUT = ROOT / "site"
PUBLISHED = os.environ.get("HATEDTV_PUBLISHED", "").rstrip("/")
STATES = "al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy".split()
J = lambda o: json.dumps(o, separators=(",", ":"))


def write(rel, data):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def channel_results():
    run = os.environ.get("RUN_HEALTH") == "1"
    if not run:
        try:
            src = PUBLISHED + "/health.json" if PUBLISHED else None
            data = json.loads(urllib.request.urlopen(src, timeout=60).read()) if src else json.loads(hub.HEALTH_FILE.read_text(encoding="utf-8"))
            hub.health.update(data)
        except Exception as ex:
            print("no earlier channel results, running the test:", ex)
            run = True
    hub.load_us()
    if run: hub.run_checks()
    hub.hstat.update(dead=sum(1 for v in hub.health.values() if not v[0]),
                     finished=max((v[1] for v in hub.health.values()), default=0), total=len(hub.health))


def main():
    if OUT.exists(): shutil.rmtree(OUT)
    channel_results()
    hub.load_epg()
    pluto_guide = guide.fetch_pluto_guide()
    guide.boot()
    guide.fetch_catalog()

    cats = [c["id"] for c in json.loads(hub.get_text("https://iptv-org.github.io/api/categories.json")) if c["id"] != "xxx"]
    paths = ["countries/us.m3u", "index.m3u", "languages/eng.m3u", "languages/spa.m3u"]
    paths += [f"categories/{c}.m3u" for c in cats] + [f"subdivisions/us-{s}.m3u" for s in STATES] + [f"services/{s[0]}.m3u" for s in hub.SERVICES]
    for p in paths:
        write("iptv/" + p, hub.serve(p, strict=True)[0])
        write("iptv-tv/" + p, hub.serve(p, strict=False)[0])

    write("guide.json", J({**pluto_guide, **hub.epg["progs"]}))
    write("config", J({"public": True, "relay": False, "playOnTv": False, "static": True}))
    write("vod/categories", J(guide.pluto["cats"]))
    for cid, items in guide.pluto["catItems"].items():
        write(f"vod/category/{cid}", J(items))
    write("vod/index.json", J([[i["id"], i["name"], i["type"], i["poster"] or i["image"]] for i in guide.pluto["items"].values()]))
    write("status", J({**hub.status(), "titles": len(guide.pluto["items"]), "shelves": len(guide.pluto["cats"])}))
    write("health.json", J(hub.health))

    shutil.copytree(ROOT / "web", OUT / "app")
    shutil.copytree(ROOT / "app" / "fonts", OUT / "app" / "fonts")
    shutil.copy2(ROOT / "app" / "images" / "logo.png", OUT / "app" / "logo.png")
    write("index.html", '<!doctype html><meta charset="utf-8"><title>Hated TV</title><meta http-equiv="refresh" content="0; url=app/">')
    write(".nojekyll", "")
    files = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(f"site built: {files} files, {len(paths)} lists, {len(guide.pluto['items'])} titles, {hub.status()['health_dead']} channels hidden")


if __name__ == "__main__":
    main()
