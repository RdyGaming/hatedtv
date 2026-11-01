(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const main = $("#main");
  const BASE = location.pathname.replace(/\/app(\/.*)?$/, "").replace(/\/$/, "");   // "" when served at the root, "/hatedtv" when served under that path
  const api = (p) => BASE + p;
  const IPTV_API = "https://iptv-org.github.io/api/categories.json";
  const LANGS = [["", "All languages"], ["eng", "English"], ["spa", "Spanish"]];
  const SERVICES = [["pluto", "Pluto TV"], ["samsung", "Samsung TV Plus"], ["plex", "Plex"], ["roku", "The Roku Channel"], ["xumo", "Xumo"], ["tubi", "Tubi"], ["stirr", "Stirr"], ["localnow", "Local Now"]];
  const STATES = "AL Alabama|AK Alaska|AZ Arizona|AR Arkansas|CA California|CO Colorado|CT Connecticut|DE Delaware|DC District of Columbia|FL Florida|GA Georgia|HI Hawaii|ID Idaho|IL Illinois|IN Indiana|IA Iowa|KS Kansas|KY Kentucky|LA Louisiana|ME Maine|MD Maryland|MA Massachusetts|MI Michigan|MN Minnesota|MS Mississippi|MO Missouri|MT Montana|NE Nebraska|NV Nevada|NH New Hampshire|NJ New Jersey|NM New Mexico|NY New York|NC North Carolina|ND North Dakota|OH Ohio|OK Oklahoma|OR Oregon|PA Pennsylvania|RI Rhode Island|SC South Carolina|SD South Dakota|TN Tennessee|TX Texas|UT Utah|VT Vermont|VA Virginia|WA Washington|WV West Virginia|WI Wisconsin|WY Wyoming"
    .split("|").map((s) => ["subdivisions/us-" + s.slice(0, 2).toLowerCase() + ".m3u", s.slice(3)]);
  const QUICK = [["categories/movies.m3u", "Movies"], ["categories/series.m3u", "TV Shows"], ["categories/kids.m3u", "Kids"], ["categories/music.m3u", "Music"], ["categories/news.m3u", "News"], ["categories/sports.m3u", "Sports"], ["countries/us.m3u", "All US channels"]];

  const load = (k, d) => { try { const v = localStorage.getItem("htv." + k); return v ? JSON.parse(v) : d; } catch { return d; } };
  const save = (k, v) => { try { localStorage.setItem("htv." + k, JSON.stringify(v)); } catch { /* storage off */ } };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clock = (sec) => new Date(sec * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).toLowerCase();

  const video = $("#video");
  const NATIVE = !!video.canPlayType("application/vnd.apple.mpegurl");
  const APPLE = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
    || (/Safari\//.test(navigator.userAgent) && !/Chrome|Chromium|Edg|OPR|Android|Firefox/.test(navigator.userAgent));
  const st = {
    cfg: { public: false, relay: true, playOnTv: true },
    lang: load("lang", "eng"), langSet: null, favs: load("favs", []), last: load("last", null),
    guide: {}, lists: {}, genres: null, list: [], idx: -1, vodCats: null, vodItems: {},
  };

  /* ---------- data ---------- */
  function parseM3U(text) {
    const out = []; let cur = null;
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (line.startsWith("#EXTINF:")) {
        const a = (k) => (line.match(new RegExp(k + '="([^"]*)"')) || [])[1] || "";
        cur = { title: (line.match(/,([^"]*)$/) || [])[1]?.trim() || "", group: a("group-title"), tvgid: a("tvg-id").split("@")[0], gkey: a("hated-guide"), web: a("hated-web"), ua: "", ref: "", url: "", pluto: "" };
      } else if (line.startsWith("#EXTVLCOPT:") && cur) {
        const v = line.slice(line.indexOf("=") + 1);
        if (line.includes("http-user-agent")) cur.ua = v;
        if (line.includes("http-referrer")) cur.ref = v;
      } else if (line && !line.startsWith("#") && cur) {
        cur.url = line; cur.pluto = (line.match(/plu-([0-9a-f]+)\.m3u8/) || [])[1] || ""; out.push(cur); cur = null;
      }
    }
    return out;
  }
  async function getList(path) {
    if (!st.lists[path]) {
      try { const r = await fetch(api("/iptv/" + path)); st.lists[path] = r.ok ? parseM3U(await r.text()) : []; } catch { st.lists[path] = []; }
    }
    return st.lists[path];
  }
  async function loadLangSet() {
    st.langSet = null;
    if (!st.lang) return;
    st.langSet = new Set((await getList("languages/" + st.lang + ".m3u")).map((c) => c.url));
  }
  /* Which channels this device can play: everything when the server relays streams; on the public site a built-in
     player can load streams marked hated-web="0", except Pluto, which only Apple's player handles. Chrome's built-in player fails on it. */
  function webOk(c) {
    if (st.cfg.relay) return true;
    if (c.ua || c.ref) return false;
    if (c.web !== "0" || (st.cfg.plutoRelay && c.pluto)) return true;
    return NATIVE && (APPLE || !(c.pluto || /pluto\.tv/.test(c.url)));
  }
  const shown = (list) => list.filter((c) => (!st.lang || !st.langSet || st.langSet.has(c.url)) && webOk(c));
  async function loadGuide() { try { st.guide = await (await fetch(api("/guide.json"))).json(); } catch { /* keep old */ } }
  const progs = (c) => (c.gkey && st.guide[c.gkey]) || (c.pluto && st.guide[c.pluto]) || (c.tvgid && st.guide[c.tvgid]) || null;
  function nowNext(c) {
    const p = progs(c); if (!p) return null;
    const t = Date.now() / 1000;
    for (let i = 0; i < p.length; i++) {
      const [s, d, title] = p[i];
      if (t >= s && t < s + d) return { now: title, s, e: s + d, next: p[i + 1] ? p[i + 1][2] : "" };
    }
    return null;
  }

  /* ---------- player ---------- */
  let hls = null;
  function relayUrl(c) { const q = new URLSearchParams({ u: c.url }); if (c.ua) q.set("ua", c.ua); if (c.ref) q.set("ref", c.ref); return api("/p?" + q); }
  function showMsg(t) { const m = $("#vmsg"); m.textContent = t; m.hidden = !t; }
  /* Order of attempts: built-in player, then hls.js when the browser may load the stream, then the server's relay when it has one. */
  function attach(c, viaRelay, skipNative) {
    showMsg("");
    if (hls) { hls.destroy(); hls = null; }
    const relay = st.cfg.relay && (viaRelay || !!c.ua || !!c.ref);
    const viaPluto = !st.cfg.relay && !!st.cfg.plutoRelay && !!c.pluto;
    const src = relay ? relayUrl(c) : viaPluto ? `${st.cfg.plutoRelay}/pl/${c.pluto}.m3u8` : c.url;
    const canHls = !!(window.Hls && Hls.isSupported());
    const fail = () => {
      if (!relay && st.cfg.relay) attach(c, true);
      else showMsg("This channel isn't playing right now. Try Next.");
    };
    if (/\.mp4(\?|$)/i.test(c.url) || (NATIVE && !relay && !viaPluto && !skipNative) || !canHls) {
      video.onerror = () => { if (NATIVE && !skipNative && canHls && (st.cfg.relay || c.web !== "0")) attach(c, viaRelay, true); else fail(); };
      video.src = src; video.play().catch(() => {}); return;
    }
    video.onerror = null;
    hls = new Hls();
    hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) { hls.destroy(); hls = null; fail(); } });
    hls.loadSource(src); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
  }
  function play(list, i) {
    if (!list.length) return;
    i = (i + list.length) % list.length;
    st.list = list; st.idx = i;
    const c = list[i];
    st.last = { title: c.title, group: c.group, url: c.url, ua: c.ua, ref: c.ref, pluto: c.pluto, tvgid: c.tvgid, gkey: c.gkey, web: c.web };
    save("last", st.last);
    $("#player").hidden = false;
    attach(c, false);
    paintNow();
    document.querySelectorAll(".g-row").forEach((r) => r.classList.toggle("on", +r.dataset.i === i));
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function paintNow() {
    const c = st.list[st.idx]; if (!c) return;
    const nn = nowNext(c);
    $("#npCh").textContent = c.title;
    $("#npNow").textContent = nn ? `Now: ${nn.now}  ${clock(nn.s)} to ${clock(nn.e)}` : (c.group || "Live");
    $("#npNext").textContent = nn && nn.next ? "Next: " + nn.next : "";
    $("#fav").textContent = isFav(c) ? "Unfavorite" : "Favorite";
  }
  function closePlayer() { if (hls) { hls.destroy(); hls = null; } video.removeAttribute("src"); video.load(); $("#player").hidden = true; }
  $("#prev").onclick = () => play(st.list, st.idx - 1);
  $("#next").onclick = () => play(st.list, st.idx + 1);
  $("#close").onclick = closePlayer;
  $("#fav").onclick = () => { toggleFav(st.list[st.idx]); paintNow(); };
  document.addEventListener("keydown", (e) => {
    if ($("#player").hidden || /INPUT|TEXTAREA/.test(document.activeElement?.tagName || "")) return;
    if (e.key === "ArrowUp" || e.key === "PageUp") { e.preventDefault(); play(st.list, st.idx - 1); }
    else if (e.key === "ArrowDown" || e.key === "PageDown") { e.preventDefault(); play(st.list, st.idx + 1); }
    else if (e.key === "Escape") closePlayer();
  });

  /* ---------- favorites ---------- */
  const isFav = (c) => st.favs.some((f) => f.url === c.url);
  function toggleFav(c) {
    if (!c) return;
    if (isFav(c)) st.favs = st.favs.filter((f) => f.url !== c.url);
    else st.favs.push({ title: c.title, group: c.group, url: c.url, ua: c.ua, ref: c.ref, pluto: c.pluto, tvgid: c.tvgid, gkey: c.gkey, web: c.web });
    save("favs", st.favs);
    toast(isFav(c) ? "Added to Favorites" : "Removed from Favorites");
  }

  /* ---------- guide grid (laid out like the Roku TimeGrid) ---------- */
  const PPM = () => (innerWidth < 760 ? 3 : 4.2);       // pixels per minute
  const CHW = () => (innerWidth < 760 ? 150 : 240);     // channel column width
  const HOURS = 8;
  function guideHtml(title, sub) {
    return `<div class="listhead"><div><div class="eyebrow">Live TV</div><h1>${esc(title)}</h1><div class="muted" id="gsub">${esc(sub)}</div></div>
      <input class="filter" id="flt" type="search" placeholder="Filter this list" aria-label="Filter this list"></div>
      <div class="g-info" id="ginfo"><div class="g-ich">Pick a show or channel</div><div class="g-inow"></div><div class="g-inext"></div></div>
      <div class="g-scroll" id="gscroll"><div class="g-inner" id="ginner"></div></div>`;
  }
  function drawGuide(list) {
    const ppm = PPM(), chw = CHW();
    const now = Date.now() / 1000;
    const start = now - (now % 1800);
    const end = start + HOURS * 3600;
    const px = (t) => ((t - start) / 60) * ppm;
    const width = chw + px(end);
    const inner = $("#ginner");
    inner.style.width = width + "px";
    let head = `<div class="g-times" style="width:${width}px"><div class="g-corner" style="width:${chw}px">${new Date().toLocaleDateString([], { weekday: "long" })}</div>`;
    for (let t = start; t < end; t += 1800) head += `<div class="g-time" style="left:${chw + px(t)}px;width:${30 * ppm}px">${clock(t)}</div>`;
    head += `</div><div class="g-now" style="left:${chw + px(now)}px"></div>`;
    inner.innerHTML = head + `<div id="grows"></div><div id="gmore" class="g-more"></div>`;
    let drawn = 0;
    const rowsBox = $("#grows");
    const more = () => {
      const html = [];
      for (const [k, c] of list.slice(drawn, drawn + 120).entries()) {
        const i = drawn + k, p = progs(c);
        let blocks = "";
        if (p && p.length) {
          for (const [j, [s, d, t]] of p.entries()) {
            if (s + d <= start || s >= end) continue;
            const l = Math.max(px(s), 0), r = Math.min(px(s + d), px(end));
            blocks += `<button class="g-prog${now >= s && now < s + d ? " live" : ""}" type="button" data-i="${i}" data-p="${j}" style="left:${l}px;width:${Math.max(r - l - 3, 8)}px">${esc(t)}</button>`;
          }
        }
        if (!blocks) blocks = `<button class="g-prog none" type="button" data-i="${i}" style="left:0;width:${px(end) - 3}px">${esc(c.title)}<span>${esc(c.group || "")}</span></button>`;
        html.push(`<div class="g-row${st.list === list && st.idx === i ? " on" : ""}" data-i="${i}">
          <div class="g-ch" style="width:${chw}px" data-i="${i}" tabindex="0" role="button" aria-label="Play ${esc(c.title)}"><span class="num">${i + 1}</span><span class="name">${esc(c.title)}</span>
            <button class="star${isFav(c) ? " on" : ""}" data-star="${i}" type="button" aria-label="Favorite">&#9733;</button></div>
          <div class="g-progs" style="width:${px(end)}px">${blocks}</div></div>`);
      }
      rowsBox.insertAdjacentHTML("beforeend", html.join(""));
      drawn += 120;
      $("#gmore").textContent = drawn < list.length ? `Loading more... (${drawn} of ${list.length})` : "";
    };
    more();
    const io = new IntersectionObserver((es) => { if (es[0].isIntersecting && drawn < list.length) more(); }, { root: $("#gscroll"), rootMargin: "400px" });
    io.observe($("#gmore"));
    const info = (i, j) => {
      const c = list[i]; if (!c) return;
      const p = progs(c), nn = nowNext(c);
      $("#ginfo .g-ich").textContent = `${i + 1}  ${c.title}`;
      if (p && j != null && p[j]) { $("#ginfo .g-inow").textContent = p[j][2]; $("#ginfo .g-inext").textContent = `${clock(p[j][0])} to ${clock(p[j][0] + p[j][1])}`; }
      else if (nn) { $("#ginfo .g-inow").textContent = `Now: ${nn.now}  ${clock(nn.s)} to ${clock(nn.e)}`; $("#ginfo .g-inext").textContent = nn.next ? "Next: " + nn.next : ""; }
      else { $("#ginfo .g-inow").textContent = c.group || "Live channel"; $("#ginfo .g-inext").textContent = ""; }
    };
    inner.onclick = (e) => {
      const s = e.target.closest("[data-star]");
      if (s) { e.stopPropagation(); toggleFav(list[+s.dataset.star]); s.classList.toggle("on", isFav(list[+s.dataset.star])); return; }
      const b = e.target.closest("[data-i]"); if (b) play(list, +b.dataset.i);
    };
    inner.onmouseover = inner.onfocusin = (e) => { const b = e.target.closest("[data-i]"); if (b) info(+b.dataset.i, b.dataset.p != null ? +b.dataset.p : null); };
    inner.onkeydown = (e) => { if (e.key === "Enter" && e.target.matches(".g-ch")) play(list, +e.target.dataset.i); };
    if (list.length) info(0, null);
  }
  function showGuide(title, all, note) {
    const list = shown(all);
    const hiddenHere = all.length - list.length;
    main.innerHTML = guideHtml(title, `${list.length} channels${note ? " . " + note : ""}${!st.cfg.relay && hiddenHere ? ` . ${hiddenHere} more play in the phone or Roku app` : ""}`);
    drawGuide(list);
    $("#flt").oninput = (e) => { const q = e.target.value.trim().toLowerCase(); drawGuide(q ? list.filter((c) => c.title.toLowerCase().includes(q)) : list); };
  }
  async function openList(path, title) {
    main.innerHTML = `<div class="muted">Loading ${esc(title)}...</div>`;
    showGuide(title, await getList(path), LANGS.find((l) => l[0] === st.lang)[1]);
  }

  /* ---------- home and menus ---------- */
  const ART = {
    live: " ┌──────────┐\n │ ▓▓▒▒░░·· │\n │ ▓▓▒▒░░·· │\n └───┬──┬───┘",
    vod: " ▌▐▀▀▀▀▀▀▀▀▌▐\n ▌▐  ▶     ▌▐\n ▌▐        ▌▐\n ▌▐▄▄▄▄▄▄▄▄▌▐",
    favs: "  ▄██▄  ▄██▄\n  ██████████\n   ▀██████▀\n     ▀██▀",
    search: "   ┌─────┐\n   │ ▒▒▒ │\n   └──┬──┘\n       ╲╲",
    states: "  ·─────────·\n  │ ░▒▓ ·▓▒ │\n  │ ·▒░ ▓░· │\n  ·─────────·",
  };
  const chips = (items) => items.map(([p, t]) => `<a href="#list/${encodeURIComponent(p)}/${encodeURIComponent(t)}">${esc(t)}</a>`).join("");
  function viewHome() {
    const l = st.last, nn = l ? nowNext(l) : null;
    main.innerHTML = `
      <section class="hero"><div><div class="eyebrow">On now // last watched</div>
        <h1>${l ? esc(nn ? nn.now : l.title) : "Pick something to watch"}</h1>
        <div class="time">${l ? esc(nn ? `${clock(nn.s)} to ${clock(nn.e)} . ${l.title}` : l.title) : "Free US live TV with a guide, no sign-in"}</div>
        ${nn && nn.next ? `<div class="muted">Next: ${esc(nn.next)}</div>` : ""}</div>
        ${l ? `<button class="btn primary" id="resume" type="button">Resume</button>` : ""}</section>
      <section class="cards">
        <a class="card" href="#live"><pre>${ART.live}</pre><div><b>Live TV</b><br><span>US channels</span></div></a>
        <a class="card" href="#vod"><pre>${ART.vod}</pre><div><b>On Demand</b><br><span>Movies and shows on Pluto</span></div></a>
        <a class="card" href="#favs"><pre>${ART.favs}</pre><div><b>Favorites</b><br><span>${st.favs.length} saved</span></div></a>
        <a class="card" href="#search"><pre>${ART.search}</pre><div><b>Search</b><br><span>Channels and titles</span></div></a>
        <a class="card" href="#states"><pre>${ART.states}</pre><div><b>US States</b><br><span>50 states and DC</span></div></a>
      </section>
      <section><div class="eyebrow">Browse</div><div class="chips" style="margin-top:8px">${chips(QUICK)}<a href="#live">All genres</a><a href="#live">Services</a></div></section>`;
    const r = $("#resume"); if (r) r.onclick = () => play([st.last], 0);
  }
  async function viewLive() {
    if (!st.genres) { try { st.genres = (await (await fetch(IPTV_API)).json()).filter((g) => g.id !== "xxx").map((g) => ["categories/" + g.id + ".m3u", g.name]); } catch { st.genres = QUICK.slice(0, 6); } }
    main.innerHTML = `<div><div class="eyebrow">Live TV</div><h1>Pick a list</h1></div>
      <section><h2>Quick picks</h2><div class="chips">${chips(QUICK)}</div></section>
      <section><h2>Services</h2><div class="chips">${chips(SERVICES.map(([s, n]) => ["services/" + s + ".m3u", n]))}</div></section>
      <section><h2>Genres</h2><div class="chips">${chips(st.genres)}</div></section>
      <section><h2>US states</h2><div class="chips">${chips(STATES)}</div></section>`;
  }
  function viewStates() { main.innerHTML = `<div><div class="eyebrow">Live TV</div><h1>US states</h1></div><div class="chips">${chips(STATES)}</div>`; }
  function viewFavs() {
    if (!st.favs.length) { main.innerHTML = `<div><div class="eyebrow">Favorites</div><h1>No favorites yet</h1><p class="muted">Tap the star next to any channel to keep it here.</p></div>`; return; }
    showGuide("Favorites", st.favs.slice());
  }

  /* ---------- search ---------- */
  async function viewSearch(q) {
    main.innerHTML = `<div><div class="eyebrow">Search</div><h1>Find a channel or title</h1></div>
      <input class="filter" id="q" type="search" placeholder="Channel, movie or show" aria-label="Search" value="${esc(q || "")}" autofocus>
      <section id="sr"></section>`;
    let t;
    const run = async (text) => {
      const box = $("#sr"); text = text.trim().toLowerCase();
      if (text.length < 2) { box.innerHTML = ""; return; }
      box.innerHTML = `<div class="muted">Searching...</div>`;
      const chans = shown(await getList("index.m3u")).filter((c) => c.title.toLowerCase().includes(text)).slice(0, 200);
      if (!st.vodIndex) { try { st.vodIndex = await (await fetch(api("/vod/index.json"))).json(); } catch { st.vodIndex = []; } }
      const titles = st.vodIndex.filter((r) => r[1].toLowerCase().includes(text)).slice(0, 120)
        .map(([id, name, type, poster]) => ({ id, name, type, poster, image: poster, summary: "", genre: "", rating: "", minutes: 0 }));
      box.innerHTML = `<h2>Channels (${chans.length})</h2><div class="g-scroll short"><div class="g-inner" id="ginner"></div></div><div class="g-info" id="ginfo" hidden><div class="g-ich"></div><div class="g-inow"></div><div class="g-inext"></div></div>
        <h2 style="margin-top:20px">On Demand (${titles.length})</h2><div class="posters" id="sposters"></div>`;
      drawGuide(chans);
      drawPosters($("#sposters"), titles);
    };
    $("#q").oninput = (e) => { clearTimeout(t); t = setTimeout(() => run(e.target.value), 350); };
    if (q) run(q);
  }

  /* ---------- on demand: titles open in Pluto, like the Roku app ---------- */
  const plutoUrl = (d, seriesId) => d.type === "movie" ? "https://pluto.tv/us/on-demand/movies/" + d.id : "https://pluto.tv/us/on-demand/series/" + (seriesId || d.id);
  async function viewVod(shelf) {
    if (!st.vodCats) { try { st.vodCats = await (await fetch(api("/vod/categories"))).json(); } catch { st.vodCats = []; } }
    if (!st.vodCats.length) { main.innerHTML = `<h1>On Demand is loading</h1><p class="muted">The catalog isn't ready yet. Try again in a minute.</p>`; return; }
    const id = shelf || st.vodCats[0].id;
    main.innerHTML = `<div><div class="eyebrow">On Demand // opens in Pluto TV</div><h1>${esc((st.vodCats.find((c) => c.id === id) || {}).name || "")}</h1></div>
      <div class="shelves">${st.vodCats.map((c) => `<button type="button" data-shelf="${esc(c.id)}" class="${c.id === id ? "on" : ""}">${esc(c.name)}</button>`).join("")}</div>
      <div class="posters" id="posters"><div class="muted">Loading...</div></div>`;
    main.querySelector(".shelves").onclick = (e) => { const b = e.target.closest("[data-shelf]"); if (b) location.hash = "vod/" + b.dataset.shelf; };
    main.querySelector(".shelves .on")?.scrollIntoView({ inline: "center", block: "nearest" });
    if (!st.vodItems[id]) { try { st.vodItems[id] = await (await fetch(api("/vod/category/" + id))).json(); } catch { st.vodItems[id] = []; } }
    drawPosters($("#posters"), st.vodItems[id]);
  }
  function drawPosters(box, items) {
    const direct = !st.cfg.playOnTv;
    box.innerHTML = items.map((d, i) => direct && d.type === "movie"
      ? `<a class="poster" href="${esc(plutoUrl(d))}" target="_blank" rel="noopener"><img loading="lazy" alt="" src="${esc(d.poster || d.image)}"><span>${esc(d.name)}</span></a>`
      : `<button class="poster" type="button" data-p="${i}"><img loading="lazy" alt="" src="${esc(d.poster || d.image)}"><span>${esc(d.name)}</span></button>`).join("") || `<div class="muted">Nothing here right now.</div>`;
    box.onclick = (e) => { const b = e.target.closest("[data-p]"); if (b) openTitle(items[+b.dataset.p]); };
  }
  function sheet(html) {
    const s = document.createElement("div"); s.className = "sheet"; s.innerHTML = `<div role="dialog" aria-modal="true">${html}</div>`;
    s.onclick = (e) => { if (e.target === s || e.target.closest("[data-close]")) s.remove(); };
    document.body.appendChild(s); return s;
  }
  const meta = (d) => [d.type === "series" ? "Series" : "", d.genre, d.rating, d.minutes && d.type !== "series" ? d.minutes + " min" : ""].filter(Boolean).join("  .  ");
  const tvBtn = (attr) => st.cfg.playOnTv ? `<button class="btn primary" ${attr} type="button">Play on TV</button>` : "";
  async function openTitle(d) {
    if (d.type === "series") {
      const s = sheet(`<h2>${esc(d.name)}</h2><div class="muted">${esc(meta(d))}</div><p>${esc(d.summary)}</p><div class="eps"><div class="muted">Loading episodes...</div></div><button class="btn" data-close type="button">Close</button>`);
      let eps = [];
      try { eps = await (await fetch(api("/vod/series/" + d.id))).json(); } catch { /* server busy */ }
      s.querySelector(".eps").innerHTML = eps.map((e, i) => `<div class="ep"><span>S${esc(e.season)} E${esc(e.number)}  ${esc(e.name)}</span><span>${tvBtn(`data-tv="${i}"`)} <a class="btn" target="_blank" rel="noopener" href="${esc(plutoUrl(e, d.id))}">Watch on Pluto</a></span></div>`).join("") || `<div class="muted">No episodes available right now.</div>`;
      s.querySelector(".eps").onclick = (e) => { const b = e.target.closest("[data-tv]"); if (b) sendToTv(eps[+b.dataset.tv]); };
      return;
    }
    const s = sheet(`<h2>${esc(d.name)}</h2><div class="muted">${esc(meta(d))}</div><p>${esc(d.summary)}</p>
      <div class="np-btns">${tvBtn("data-tvplay")}<a class="btn" target="_blank" rel="noopener" href="${esc(plutoUrl(d))}">Watch on Pluto</a><button class="btn" data-close type="button">Close</button></div>`);
    s.querySelector("[data-tvplay]")?.addEventListener("click", () => sendToTv(d));
  }
  async function sendToTv(d) {
    try { const r = await fetch(api(`/vod/open?id=${encodeURIComponent(d.id)}&type=${encodeURIComponent(d.type)}`)); toast(r.ok ? "Sent to the TV" : "The TV didn't answer"); }
    catch { toast("The TV didn't answer"); }
  }

  /* ---------- shell ---------- */
  let tt;
  function toast(t) { const el = $("#toast"); el.textContent = t; el.hidden = false; clearTimeout(tt); tt = setTimeout(() => (el.hidden = true), t.length > 40 ? 4000 : 1800); }
  function paintLang() { $("#lang").textContent = "Language: " + LANGS.find((l) => l[0] === st.lang)[1]; }
  $("#lang").onclick = async () => {
    const i = LANGS.findIndex((l) => l[0] === st.lang);
    st.lang = LANGS[(i + 1) % LANGS.length][0]; save("lang", st.lang); paintLang();
    await loadLangSet(); route();
  };
  let installEvt = null;
  const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (ios && !installed) $("#install").hidden = false;
  addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; $("#install").hidden = false; });
  addEventListener("appinstalled", () => { installEvt = null; $("#install").hidden = true; });
  $("#install").onclick = async () => {
    if (!installEvt) return toast("Tap the Share button, then Add to Home Screen");
    installEvt.prompt();
    await installEvt.userChoice;
    installEvt = null; $("#install").hidden = true;
  };
  function tick() { $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).toLowerCase(); if (!$("#player").hidden) paintNow(); }
  async function route() {
    const [view, a, b] = location.hash.slice(1).split("/");
    document.querySelectorAll(".tabs a").forEach((x) => x.classList.toggle("on", x.dataset.tab === (view || "home") || (view === "list" && x.dataset.tab === "live")));
    if (view === "live") return viewLive();
    if (view === "list" && a) return openList(decodeURIComponent(a), decodeURIComponent(b || "Channels"));
    if (view === "states") return viewStates();
    if (view === "favs") return viewFavs();
    if (view === "search") return viewSearch(a ? decodeURIComponent(a) : "");
    if (view === "vod") return viewVod(a);
    return viewHome();
  }

  window.addEventListener("hashchange", route);
  paintLang(); tick(); setInterval(tick, 30000);
  (async () => {
    try { st.cfg = await (await fetch(api("/config"))).json(); } catch { /* defaults */ }
    await Promise.all([loadGuide(), loadLangSet()]);
    route();
  })();
  setInterval(loadGuide, 20 * 60 * 1000);
})();
