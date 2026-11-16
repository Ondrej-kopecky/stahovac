"""Testy oprav Webshare stahovače — běží v samostatném kontejneru nad dočasnými soubory."""
# Spuštění (z kořene repozitáře, image stahovac z docker compose build): docker run --rm --entrypoint python -v "$PWD/tests:/t:ro" stahovac /t/test_fixes.py
import asyncio, os, subprocess, sys, tempfile
os.environ.setdefault("INSECURE_NO_LOGIN", "1")      # testy volají API i bez hesla
sys.path.insert(0, "/app")
os.chdir("/app")
import app as A
from fastapi import HTTPException

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

def touch(d, *names):
    for n in names:
        open(os.path.join(d, n), "w").write("x")

# 1) titulky: Rocky.mkv nesmí vzít Rocky.II.mkv ani jeho titulky
d = tempfile.mkdtemp()
touch(d, "Rocky.mkv", "Rocky.II.mkv", "Rocky.cs.srt", "Rocky.nfo", "Rocky.II.cs.srt", "Rocky.II.nfo")
check("sidecars Rocky = jen jeho titulky", sorted(A._sidecars(d, "Rocky.mkv")) == ["Rocky.cs.srt", "Rocky.nfo"],
      A._sidecars(d, "Rocky.mkv"))
root = tempfile.mkdtemp(); sub = os.path.join(root, "x"); os.makedirs(sub)
for f in os.listdir(d): os.rename(os.path.join(d, f), os.path.join(sub, f))
A._delete_file(os.path.join(sub, "Rocky.mkv"), root)
check("smazání Rocky.mkv nechá Rocky II", sorted(os.listdir(sub)) == ["Rocky.II.cs.srt", "Rocky.II.mkv", "Rocky.II.nfo"],
      os.listdir(sub))

# 1b) kolize aria2: Film.mkv vs Film.1.mkv (+ Film.1.srt patří k Film.1)
d = tempfile.mkdtemp(); touch(d, "Film.mkv", "Film.1.mkv", "Film.1.srt", "Film.srt")
check("Film.mkv nevezme Film.1.mkv ani Film.1.srt", A._sidecars(d, "Film.mkv") == ["Film.srt"], A._sidecars(d, "Film.mkv"))
A._delete_file(os.path.join(d, "Film.mkv"), os.path.dirname(d))
check("po smazání Film.mkv zůstane Film.1", sorted(os.listdir(d)) == ["Film.1.mkv", "Film.1.srt"], os.listdir(d))

# 1c) prázdná složka série se uklidí, kořen ne
root = tempfile.mkdtemp(); s = os.path.join(root, "Show", "Season 01"); os.makedirs(s); touch(s, "S01E01.mkv")
A._delete_file(os.path.join(s, "S01E01.mkv"), root)
check("prázdná série i seriál uklizeny, kořen zůstal", os.path.isdir(root) and os.listdir(root) == [], os.listdir(root))

# 2) on_complete: existující soubor se nepřepíše
sys.path.insert(0, "/app"); import on_complete as OC
d = tempfile.mkdtemp(); touch(d, "Spider.mkv", "Spider (2).mkv")
check("free_name → (3)", OC.free_name(os.path.join(d, "Spider.mkv")) == os.path.join(d, "Spider (3).mkv"))
check("free_name bez kolize beze změny", OC.free_name(os.path.join(d, "Jiny.mkv")) == os.path.join(d, "Jiny.mkv"))

# 3) volné místo (aria2 + disk podvržené)
async def fake_aria2(client, method, *p):
    return [{"totalLength": str(20 * 1024**3), "completedLength": str(5 * 1024**3)}] if method == "tellActive" else []
A.aria2 = fake_aria2
A._free_space = lambda: 100 * 1024**3          # 100 GB volno, 15 GB rozstahováno, 10 GB rezerva → vejde se 75 GB
async def space(size):
    try:
        await A._check_space(None, size); return "ok"
    except HTTPException as e:
        return e.status_code
check("70 GB se vejde", asyncio.run(space(70 * 1024**3)) == "ok")
check("80 GB odmítnuto (507)", asyncio.run(space(80 * 1024**3)) == 507)
check("neznámá velikost nevadí", asyncio.run(space(0)) == "ok")

# 4) token: token z paměti zneplatněný → nové přihlášení → úspěch; čerstvý token + chyba → bez opakování
calls = {"login": 0, "post": 0}
async def fake_login(client):
    if A._token:
        return A._token
    calls["login"] += 1; A._token = f"tok{calls['login'] + 1}"; return A._token
class R:
    def __init__(s, t): s.text = t
class C:
    async def post(s, url, data):
        calls["post"] += 1
        if data["wst"] == "tok1":
            return R("<response><status>FATAL</status><code>X</code><message>Not logged</message></response>")
        return R("<response><status>OK</status><name>f.mkv</name><size>5</size></response>")
A.ws_login = fake_login
A._token = "tok1"                                   # starý token v cache
r = asyncio.run(A._ws_call(C(), "file_info", {"ident": "x"}))
check("token obnoven a druhý pokus prošel", r.findtext("name") == "f.mkv" and calls["login"] == 1, calls)
class C2(C):
    async def post(s, url, data):
        calls["post"] += 1
        return R("<response><status>FATAL</status><code>X</code><message>bad</message></response>")
A._token = "stary"; calls["post"] = 0
try:
    asyncio.run(A._ws_call(C2(), "file_info", {"ident": "x"})); check("trvalá chyba → výjimka", False)
except HTTPException as e:
    check("trvalá chyba → výjimka po 2 pokusech", e.status_code == 502 and calls["post"] == 2, calls)
A._token = None; calls["post"] = 0
try:
    asyncio.run(A._ws_call(C2(), "file_info", {"ident": "x"})); check("čerstvý token + chyba", False)
except HTTPException:
    check("čerstvý token + chyba → bez opakování (1 volání)", calls["post"] == 1, calls)

# 3b) fronta: aria2 zná totalLength=0, velikost se vezme z /api/add (EXPECTED_SIZE)
async def fake_aria2_q(client, method, *p):
    if method == "tellActive":
        return []
    return [{"gid": "q1", "totalLength": "0", "completedLength": "0"}]
A.aria2 = fake_aria2_q
A._free_space = lambda: 100 * 1024**3
A.EXPECTED_SIZE["q1"] = 60 * 1024**3                # ve frontě čeká 60 GB → zbývá 30 GB
check("fronta se započítá: 40 GB odmítnuto", asyncio.run(space(40 * 1024**3)) == 507)
check("fronta se započítá: 25 GB projde", asyncio.run(space(25 * 1024**3)) == "ok")

# 6) lifespan spustí sampler grafu rychlosti
started = []
async def fake_sampler():
    started.append(1)
    await asyncio.sleep(3600)
A._sample_speeds = fake_sampler
from fastapi.testclient import TestClient
with TestClient(A.app):
    import time; time.sleep(0.2)
check("lifespan spustil sampler", started == [1], started)

# 5a) přesměrování do LAN / na jiný protokol (živě přes internet)
import httpx
async def direct(url):
    async with httpx.AsyncClient() as c:
        try:
            return await A._resolve_direct(c, url)
        except HTTPException as e:
            return e.status_code, e.detail
res = asyncio.run(direct("https://httpbin.org/redirect-to?url=http://192.168.0.1/&status_code=302"))
check("redirect na 192.168.0.1 odmítnut", res[0] == 400 and "vnitřní" in res[1], res)
res = asyncio.run(direct("https://httpbin.org/redirect-to?url=file:///etc/passwd&status_code=302"))
check("redirect na file:// odmítnut", res[0] == 400, res)
res = asyncio.run(direct("http://192.168.1.10:8097/"))
check("přímá LAN URL odmítnuta", res[0] == 400, res)
res = asyncio.run(direct("http://100.64.0.1:8096/"))
check("Tailscale IP serveru odmítnuta", res[0] == 400, res)
res = asyncio.run(direct("https://httpbin.org/redirect-to?url=http://100.64.0.1:8096/&status_code=302"))
check("redirect na Tailscale IP odmítnut", res[0] == 400, res)
res = asyncio.run(direct("https://httpbin.org/redirect-to?url=https://httpbin.org/bytes/1024&status_code=302"))
check("veřejný redirect projde na cílovou URL", isinstance(res[0], str) and res[0].endswith("/bytes/1024"), res)

# 7) zrušení stahování: maže jen v .incomplete, chybové uklidí, hotové nikdy
inc = tempfile.mkdtemp(); A.INCOMPLETE = os.path.realpath(inc)
lib = tempfile.mkdtemp(); touch(lib, "film.mkv")
os.makedirs(os.path.join(inc, "serialy", "Show", "Season 01"))
part = os.path.join(inc, "serialy", "Show", "Season 01", "e01.mkv"); touch(os.path.dirname(part), "e01.mkv", "e01.mkv.aria2")
check("discard: mimo .incomplete nic", A._discard_partial(os.path.join(lib, "film.mkv")) == 0 and os.path.exists(os.path.join(lib, "film.mkv")))
os.symlink(os.path.join(lib, "film.mkv"), os.path.join(inc, "link.mkv"))
A._discard_partial(os.path.join(inc, "link.mkv"))
check("discard: symlink ven z .incomplete nic nesmaže", os.path.exists(os.path.join(lib, "film.mkv")))
os.remove(os.path.join(inc, "link.mkv"))
A._discard_partial(part)
check("discard: soubor + .aria2 + prázdné složky pryč, kořen zůstal", os.listdir(inc) == [] and os.path.isdir(inc), os.listdir(inc))

class _Req:                                          # stačí request.state.user
    class state: user = A.WEB_USER
ADMIN_REQ = _Req()

def fake_status(status, path):
    calls = []
    async def f(client, method, *p):
        calls.append(method)
        if method == "tellStatus":
            return {"status": status, "files": [{"path": path}]}
        return "OK"
    return f, calls
for status, should_delete in (("error", True), ("complete", False)):
    os.makedirs(os.path.join(inc, "filmy"), exist_ok=True)
    p = os.path.join(inc, "filmy", "x.mkv"); touch(os.path.dirname(p), "x.mkv")
    A.aria2, calls = fake_status(status, p)
    asyncio.run(A.remove("g1", ADMIN_REQ))
    check(f"remove({status}): {'smaže zbytek' if should_delete else 'nic nemaže'}", os.path.exists(p) != should_delete and "forceRemove" not in calls, calls)
    if os.path.exists(p): os.remove(p)

# 8) rodina: smí přidat stahování a sahat jen na svoje, mazat/opravovat nesmí
import base64 as _b64
A.WEB_PASS = "adminpw"; A.FAMILY = {"rodina": "rodinapw"}
A.OWNERS_FILE = os.path.join(tempfile.mkdtemp(), "owners.json"); A.OWNERS.clear()
A.OWNERS.update({"gmine": "rodina", "gadmin": "admin"})
def hdr(u, p): return {"Authorization": "Basic " + _b64.b64encode(f"{u}:{p}".encode()).decode()}
SEG, ADM = hdr("rodina", "rodinapw"), hdr("admin", "adminpw")
A.aria2, _ = fake_status("paused", "/nikde/x.mkv")
with TestClient(A.app) as c:
    check("rodina: špatné heslo 401", c.get("/api/me", headers=hdr("rodina", "spatne")).status_code == 401)
    check("rodina: rodina s heslem správce neprojde", c.get("/api/me", headers=hdr("rodina", "adminpw")).status_code == 401)
    me = c.get("/api/me", headers=SEG).json()
    check("rodina: /api/me admin=False", {k: me.get(k) for k in ("user", "admin")} == {"user": "rodina", "admin": False}, me)
    check("správce: /api/me admin=True", c.get("/api/me", headers=ADM).json()["admin"] is True)
    for m, u, kw in (("DELETE", "/api/library?rel=filmy/x.mkv", {}), ("DELETE", "/api/library/dir?rel=serialy/X", {}),
                     ("POST", "/api/identify/search", {"json": {"id": "0"*32, "kind": "movie", "name": "x"}}),
                     ("POST", "/api/identify/apply", {"json": {"id": "0"*32, "index": 0}})):
        r = c.request(m, u, headers=SEG, **kw)
        check(f"rodina: {m} {u.split('?')[0]} → 403", r.status_code == 403, r.status_code)
    r = c.request("DELETE", "/api/library?rel=../etc", headers=ADM)
    check("správce: mazání projde kontrolou role (400 na cestu, ne 403)", r.status_code == 400, r.status_code)
    for act in ("pause", "unpause", "remove"):
        check(f"rodina: {act} cizího stahování → 403", c.post(f"/api/{act}/gadmin", headers=SEG).status_code == 403)
    check("rodina: pause vlastního → 200", c.post("/api/pause/gmine", headers=SEG).status_code == 200)
    check("rodina: neznámé gid → 403", c.post("/api/remove/gnikdo", headers=SEG).status_code == 403)
    check("správce: pause cizího → 200", c.post("/api/pause/gmine", headers=ADM).status_code == 200)
    r = c.post("/api/add", headers=SEG, json={"url": "https://example.com/film.mkv", "target": "filmy"})
    check("rodina: přímá URL odmítnuta", r.status_code == 400 and "Webshare" in r.json()["detail"], r.text)
    r = c.post("/login", data={"user": "rodina", "password": "rodinapw"}, follow_redirects=False)
    check("rodina: login stránkou → cookie", r.status_code == 303 and r.headers["location"] == "/" and A.COOKIE in r.cookies, r.headers)
    tok = r.cookies[A.COOKIE]
    check("rodina: cookie nese jméno", A.check_token(tok) == "rodina")
    A.FAMILY = {}
    check("odebraný člen rodiny: cookie přestane platit", A.check_token(tok) is None)

# 9) Fastshare: odkaz → stránka na .cloud, díl bez „S“, přihlášení, cookie jen na Fastshare
check("fastshare: .cz odkaz → .cloud",
      A.parse_fastshare("https://fastshare.cz/35291866/futurama-08e05-x.mkv?utm=1") == "https://fastshare.cloud/35291866/futurama-08e05-x.mkv")
check("fastshare: bez názvu v cestě = neplatný", A.parse_fastshare("https://fastshare.cloud/35291866") is None)
check("webshare odkaz není fastshare", A.parse_fastshare("https://webshare.cz/#/file/abc") is None)
n = A.normalize_episode("Futurama_08E05_Produkty související s prohlíženými položkami.mkv")
check("díl bez S → S08E05", n == "Futurama S08E05 Produkty související s prohlíženými položkami.mkv", n)
check("díl bez S → složka seriálu", A.series_subdir(n) == "/Futurama/Season 08", A.series_subdir(n))
check("díl bez názvu: Show.1E02.mkv", A.normalize_episode("Show.1E02.mkv") == "Show S01E02.mkv", A.normalize_episode("Show.1E02.mkv"))
check("S01E02 beze změny", A.normalize_episode("Show S01E02 x.mkv") == "Show S01E02 x.mkv")
check("film beze změny", A.normalize_episode("Blade Runner 2049 (2017).mkv") == "Blade Runner 2049 (2017).mkv")
n = A.normalize_episode("South Park 13x01.mp4")
check("díl 13x01 → S13E01", n == "South Park S13E01.mp4", n)
check("díl 13x01 → složka seriálu", A.series_subdir(n).endswith("/Season 13"), A.series_subdir(n))
check("díl 2x05 s názvem", A.normalize_episode("Show.2x05.Pilot.mkv") == "Show S02E05.Pilot.mkv", A.normalize_episode("Show.2x05.Pilot.mkv"))
check("rozlišení 1920x1080 beze změny", A.normalize_episode("Film 1920x1080.mkv") == "Film 1920x1080.mkv")
check("x264 beze změny", A.normalize_episode("Film.2019.x264.mkv") == "Film.2019.x264.mkv")

import httpx
A.FS_USER, A.FS_PASS = "u", "p"
A.FS_COOKIE_FILE = os.path.join(tempfile.mkdtemp(), "fs.cookie")
open(A.FS_COOKIE_FILE, "w").write("STARA")
calls = []
def fs_handler(redirect_to):
    def h(req):
        calls.append((req.method, str(req.url), req.headers.get("cookie", "")))
        if req.url.path == "/sql.php":
            return httpx.Response(200, headers={"set-cookie": "FASTSHARE=NOVA; Domain=.fastshare.cz; Path=/"})
        if req.url.host == "fastshare.cloud":
            if "FASTSHARE=NOVA" in req.headers.get("cookie", ""):
                return httpx.Response(301, headers={"location": redirect_to})
            return httpx.Response(200, text="<html>free</html>")          # stará cookie → obyčejná stránka
        return httpx.Response(206, headers={"content-range": "bytes 0-0/787444491",
                                            "content-disposition": 'attachment; filename="Futurama_08E05_X.mkv"'})
    return h
_RealClient = httpx.AsyncClient
def use(handler):
    A.httpx.AsyncClient = lambda **kw: _RealClient(transport=httpx.MockTransport(handler), **kw)
use(fs_handler("https://s18.fastshare.cloud/download.php?id=1&x.mkv"))
link, name, size, cookie = asyncio.run(A.fs_resolve("https://fastshare.cloud/1/x.mkv"))
check("fastshare: stará cookie → nové přihlášení → odkaz",
      link.startswith("https://s18.fastshare.cloud/") and cookie == "NOVA" and size == 787444491 and name == "Futurama_08E05_X.mkv",
      (link, name, size, cookie))
check("fastshare: cookie uložena s právy 600", open(A.FS_COOKIE_FILE).read() == "NOVA"
      and oct(os.stat(A.FS_COOKIE_FILE).st_mode & 0o777) == "0o600", oct(os.stat(A.FS_COOKIE_FILE).st_mode))
calls.clear(); use(fs_handler("https://zly.example.com/steal"))
try:
    asyncio.run(A.fs_resolve("https://fastshare.cloud/1/x.mkv")); check("fastshare: cizí host odmítnut", False)
except HTTPException as e:
    check("fastshare: cizí host odmítnut", e.status_code == 502 and "zly.example.com" in e.detail, e.detail)
check("fastshare: cookie na cizí host neodešla", not any("example.com" in u for _, u, _ in calls), calls)
def nothing(req):
    if req.url.path == "/sql.php":
        return httpx.Response(200, headers={"set-cookie": "FASTSHARE=NOVA; Domain=.fastshare.cz; Path=/"})
    return httpx.Response(200, text="<html>smazano</html>")
use(nothing)
try:
    asyncio.run(A.fs_resolve("https://fastshare.cloud/1/x.mkv")); check("fastshare: smazaný soubor → chyba", False)
except HTTPException as e:
    check("fastshare: smazaný soubor → srozumitelná chyba", e.status_code == 502 and "Přímé stahování" in e.detail, e.detail)
A.httpx.AsyncClient = _RealClient

# 10) archivy: Skrýt maže jen nerozbalitelný archiv, /api/extract (heslo, zkusit znovu)
import time
A.extract.STATE_FILE = os.path.join(tempfile.mkdtemp(), "extract.json"); A.extract.LOCK_DIR = tempfile.mkdtemp()
A.extract.INCOMPLETE = A.INCOMPLETE
for state, should_delete in (("error", True), ("password", True), ("waiting", False), ("running", False), ("done", False)):
    os.makedirs(os.path.join(inc, "filmy"), exist_ok=True)
    p = os.path.join(inc, "filmy", "x.part1.rar"); touch(os.path.dirname(p), "x.part1.rar")
    A.extract.set_state("ga", state=state, path=p)
    A.aria2, calls = fake_status("complete", p)
    asyncio.run(A.remove("ga", ADMIN_REQ))
    check(f"Skrýt archiv ({state}): {'smaže' if should_delete else 'nechá'}", os.path.exists(p) != should_delete)
    if os.path.exists(p): os.remove(p)
check("add: archiv se nenormalizuje a jde do kořene cíle", A.extract.is_archive("Show_01E02_x.part1.rar"))

p = os.path.join(inc, "filmy", "y.rar"); touch(os.path.dirname(p), "y.rar")
A.FAMILY = {"rodina": "rodinapw"}                      # sekce 8 ji na konci odebrala
A.OWNERS.update({"gpw": "rodina", "gcizi": "admin", "grun": "rodina", "gven": "rodina"})
A.extract.set_state("gpw", state="password", msg="Archiv má heslo", path=p, archive=p)
A.extract.set_state("gcizi", state="password", path=p)
A.extract.set_state("grun", state="running", path=p)
A.extract.set_state("gven", state="error", path="/etc/passwd")
with TestClient(A.app) as c:
    check("extract: neznámé gid (ne archiv) → 404", c.post("/api/extract/gmine", headers=SEG, json={}).status_code == 404)
    check("extract: cizí archiv rodině → 403", c.post("/api/extract/gcizi", headers=SEG, json={}).status_code == 403)
    check("extract: už se rozbaluje → 409", c.post("/api/extract/grun", headers=SEG, json={}).status_code == 409)
    check("extract: cesta mimo .incomplete → 410", c.post("/api/extract/gven", headers=SEG, json={}).status_code == 410)
    r = c.post("/api/extract/gpw", headers=SEG, json={"password": "x"})
    check("extract: vlastní archiv s heslem → 200", r.status_code == 200, r.text)
    for _ in range(50):
        if A.extract.get_state()["gpw"]["state"] != "running": break
        time.sleep(0.1)
    check("extract: na pozadí doběhl (rozbitý soubor → chyba, ne viset)", A.extract.get_state()["gpw"]["state"] == "error",
          A.extract.get_state()["gpw"])

# 10) wishlist: každý uživatel svůj; film z wishlistu se uloží jako „Název (Rok) [tmdbid-…]“
D = A.discover
_tmp = tempfile.mkdtemp()
D.WISH_FILE, D.AVAIL_FILE = os.path.join(_tmp, "w.json"), os.path.join(_tmp, "a.json")
D.WISH.clear(); D.AVAIL.clear()
async def fake_details(client, kind, tid):
    return {"kind": kind, "id": tid, "title": "Duna", "orig": "Dune", "year": 2021, "rating": 7.8, "poster": None,
            "backdrop": None, "overview": "", "genres": [], "genre_ids": []}
async def fake_check(kind, tid): pass
async def fake_have(client): return {"movie": {"2"}, "tv": set()}
A._details, A._check_one, A._jf_tmdb_ids = fake_details, fake_check, fake_have
added = {}
async def fake_aria2_add(client, method, *p):
    if method == "addUri": added.update(p[1]); return "gnew"
    return []
async def fake_resolve(client, ident): return "https://x/f", "Dune.2021.2160p.CZ.dabing.mkv", 10 * 1024**3
async def no_space_check(client, size): pass
A.aria2, A.ws_resolve, A._check_space = fake_aria2_add, fake_resolve, no_space_check
with TestClient(A.app) as c:
    check("wishlist: přidat → 200", c.post("/api/wishlist", headers=SEG, json={"kind": "movie", "id": 1}).status_code == 200)
    c.post("/api/wishlist", headers=SEG, json={"kind": "movie", "id": 1})
    c.post("/api/wishlist", headers=SEG, json={"kind": "movie", "id": 2})
    w = c.get("/api/wishlist", headers=SEG).json()
    check("wishlist: bez duplicit, nejnovější první", [x["id"] for x in w] == [2, 1], w)
    check("wishlist: „v knihovně“ podle TMDB id z Jellyfinu", [x["have"] for x in w] == [True, False], w)
    check("wishlist: správce má svůj (prázdný)", c.get("/api/wishlist", headers=ADM).json() == [])
    check("wishlist: špatný druh → 400", c.post("/api/wishlist", headers=SEG, json={"kind": "game", "id": 1}).status_code == 400)
    c.delete("/api/wishlist/movie/2", headers=SEG)
    check("wishlist: odebrat", [x["id"] for x in c.get("/api/wishlist", headers=SEG).json()] == [1])
    check("wishlist: uložen na disk", '"rodina"' in open(D.WISH_FILE).read())
    r = c.post("/api/add", headers=SEG, json={"url": "https://webshare.cz/#/file/abc", "target": "filmy",
                                              "tmdb_id": 438631, "title": "Duna: část/1 <x>", "year": 2021})
    check("add z wishlistu: název pro Jellyfin", added.get("out") == "Duna část 1 x (2021) [tmdbid-438631].mkv", added.get("out"))
    check("add z wishlistu: žádné lomítko v názvu", "/" not in added.get("out", "/"))
    added.clear()
    c.post("/api/add", headers=SEG, json={"url": "https://webshare.cz/#/file/abc", "target": "filmy"})
    check("add bez TMDB: původní název", added.get("out") == "Dune.2021.2160p.CZ.dabing.mkv", added.get("out"))

# 11) brzda: kouká se v Jellyfinu → aria2 limit, /data/playing; minutu po konci uvolnit; Jellyfin pryč = bez brzdy
calls, seq = [], []
async def fake_aria2_t(client, method, *p):
    calls.append((method, p)); return "OK"
async def fake_playing(client): return seq.pop(0)
A.aria2, A._jf_playing = fake_aria2_t, fake_playing
A.PLAYBACK.update(on=False, n=0, limit=None)
os.makedirs(os.path.dirname(A.extract.PLAYING_FILE), exist_ok=True)
lim = lambda: [c[1][0]["max-overall-download-limit"] for c in calls if c[0] == "changeGlobalOption"]
async def run(steps):
    last = 0.0
    for n, now in steps:
        seq.append(n); last = await A._playback_step(None, last, now)
    return last
asyncio.run(run([(0, 1000)]))
check("brzda: nikdo nekouká → bez limitu", lim() == ["0"] and not A.PLAYBACK["on"], lim())
asyncio.run(run([(0, 1010)]))
check("brzda: beze změny → aria2 se znovu nevolá", lim() == ["0"], lim())
last = asyncio.run(run([(1, 1020)]))
check("brzda: kouká se → 40M", lim() == ["0", "40M"] and A.PLAYBACK["on"] and A.PLAYBACK["n"] == 1, lim())
check("brzda: značka pro rozbalování", os.path.exists(A.extract.PLAYING_FILE))
async def run2():
    l = last
    for n, now in [(0, 1050), (0, 1079)]:
        seq.append(n); l = await A._playback_step(None, l, now)
    r1 = (A.PLAYBACK["on"], lim()[-1])
    seq.append(0); await A._playback_step(None, l, 1081)
    return r1
r1 = asyncio.run(run2())
check("brzda: do minuty po konci drží", r1 == (True, "40M"), r1)
check("brzda: po minutě uvolní + smaže značku", lim()[-1] == "0" and not A.PLAYBACK["on"] and not os.path.exists(A.extract.PLAYING_FILE), lim())
asyncio.run(run([(1, 2000)])); asyncio.run(run([(None, 2010)]))
check("brzda: Jellyfin nedostupný → bez brzdy", lim()[-1] == "0" and not A.PLAYBACK["on"], lim())
check("brzda: počítá jen přehrávající relace",
      A._count_playing([{"NowPlayingItem": {"Id": 1}}, {"UserName": "rodina"}, {"NowPlayingItem": {"Id": 2}}]) == 2)
check("brzda: lifespan spouští hlídače", "_playback_guard" in open("/app/app.py").read().split("tasks = [")[1].split("]")[0])

print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
