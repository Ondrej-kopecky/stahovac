"""
Webshare stahovač — vlož odkaz z webshare.cz (nebo fastshare.cz), server ho pod VIP účtem stáhne
(aria2, 16 paralelních spojení) do Media/filmy nebo Media/serialy.

Tok:  URL → ident → Webshare API (salt → login → file_link) → přímý link → aria2 RPC → /downloads/<cíl>
Fastshare: stránka souboru s cookie FASTSHARE → přesměrování na download.php → aria2 (s hlavičkou Cookie)
"""
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import secrets
import shutil

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from md5crypt import md5crypt
import discover
import extract
from extract import normalize_episode, series_subdir

WS_API = "https://webshare.cz/api"
WS_USER = os.environ.get("WS_USER", "")
WS_PASS = os.environ.get("WS_PASS", "")

ARIA2_RPC = os.environ.get("ARIA2_RPC", "http://127.0.0.1:6800/jsonrpc")
ARIA2_SECRET = os.environ.get("ARIA2_SECRET", "")
DOWNLOAD_ROOT = "/downloads"
TARGETS = {"filmy": "filmy", "serialy": "serialy"}
MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".webm"}   # titulky (.srt…) se nevypisují, mažou se s videem

JF_URL = os.environ.get("JELLYFIN_URL", "")
JF_TOKEN = os.environ.get("JELLYFIN_TOKEN", "")
JF_HEADERS = {"Authorization": f'MediaBrowser Token="{JF_TOKEN}"'}
# Pod jakou cestou vidí Jellyfin stejnou složku, jakou tu máme jako /downloads (výchozí /media)
JF_MEDIA = extract.JF_MEDIA_PATH + "/"
# Adresa Jellyfinu pro tlačítko „Přehrát v Jellyfinu“ v prohlížeči (prázdné = stejný počítač, port 8096)
JF_PUBLIC_URL = os.environ.get("JELLYFIN_PUBLIC_URL", "").rstrip("/")

# Přihlášení: vlastní login stránka + podepsaná cookie (HMAC), volitelně „zapamatovat" na 90 dní.
# Podpisový klíč je v /data/session.key (persistentní) → restart kontejneru neodhlásí.
# Basic auth (curl/skripty) zůstává jako alternativa.
WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASS = os.environ.get("WEB_PASS", "")
# Jen pro vývoj a testy: bez WEB_PASS jinak stránka odpoví 503
NO_LOGIN = os.environ.get("INSECURE_NO_LOGIN") == "1"
SESSION_KEY_FILE = "/data/session.key"
COOKIE = "ws_session"
REMEMBER_DAYS = 90
# Další uživatelé (třeba rodina): smí přidat stahování a pauznout/zrušit SVOJE stahování. Mazat z knihovny,
# opravovat rozpoznání, zadávat přímé URL ani sahat na cizí stahování nesmí.
# FAMILY_USERS="jmeno:heslo,jmeno:heslo" v .env (nepovinné).
FAMILY = dict(x.split(":", 1) for x in os.environ.get("FAMILY_USERS", "").split(",") if ":" in x)
FAMILY.pop(WEB_USER, None)


def _session_key() -> bytes:
    try:
        with open(SESSION_KEY_FILE, "rb") as f:
            k = f.read().strip()
            if len(k) >= 32:
                return k
    except FileNotFoundError:
        pass
    k = secrets.token_hex(32).encode()
    os.makedirs(os.path.dirname(SESSION_KEY_FILE), exist_ok=True)
    with open(SESSION_KEY_FILE, "wb") as f:
        f.write(k)
    os.chmod(SESSION_KEY_FILE, 0o600)
    return k


_SESSION_KEY = _session_key()


def _sign(payload: str) -> str:
    import hmac
    return hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()


def make_token(user: str, days: float) -> str:
    exp = int(time.time() + days * 86400)
    payload = f"{user}|{exp}"
    return f"{payload}|{_sign(payload)}"


def _pw_ok(user: str, pw: str) -> bool:
    want = WEB_PASS if user == WEB_USER else FAMILY.get(user)
    return bool(want) and secrets.compare_digest(pw.encode(), want.encode())


def check_token(token: str | None) -> str | None:
    """Platná cookie → jméno uživatele, jinak None."""
    if not token or token.count("|") != 2:
        return None
    user, exp, sig = token.split("|")
    payload = f"{user}|{exp}"
    ok = (secrets.compare_digest(sig, _sign(payload)) and (user == WEB_USER or user in FAMILY)
          and exp.isdigit() and int(exp) > time.time())
    return user if ok else None


def _basic_user(hdr: str) -> str | None:
    if not hdr.startswith("Basic "):
        return None
    try:
        user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    return user if _pw_ok(user, pw) else None


def is_admin(request: Request) -> bool:
    return getattr(request.state, "user", None) == WEB_USER


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(403, "Tohle smí jen správce")


@asynccontextmanager
async def lifespan(_app):
    # lifespan místo zastaralého @app.on_event("startup") — sampler plní graf rychlosti
    tasks = [asyncio.create_task(_sample_speeds()), asyncio.create_task(_wish_watch()),
             asyncio.create_task(_playback_guard()), asyncio.create_task(_resume_held())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Webshare stahovač", lifespan=lifespan)


# Ikony a manifest pro „Přidat na plochu" — bez loginu (prohlížeč je stahuje bez cookie)
_ICONS = {f"/icons/{n}": f"static/icons/{n}" for n in
          ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not WEB_PASS and not NO_LOGIN:
        # Stránka umí mazat soubory → bez hesla ji nikomu neukážeme
        return Response("Nastav WEB_PASS v .env (heslo na stránku stahovače) a restartuj kontejner.",
                        status_code=503, media_type="text/plain; charset=utf-8")
    request.state.user = WEB_USER if not WEB_PASS else None      # NO_LOGIN (jen vývoj/testy) = správce
    if WEB_PASS and request.url.path not in ("/login", "/logout", "/favicon.svg", "/manifest.webmanifest", *_ICONS):
        user = check_token(request.cookies.get(COOKIE)) or _basic_user(request.headers.get("authorization", ""))
        if not user:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Nepřihlášen"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        request.state.user = user
    return await call_next(request)


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


# Ochrana proti hádání hesla: po 5 neúspěších z jedné IP 15 min blok (v paměti, restart resetuje).
_fails: dict[str, list[float]] = {}
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    return len(_fails[ip]) >= FAIL_LIMIT


@app.post("/login")
async def login(request: Request):
    ip = _client_ip(request)
    if _blocked(ip):
        return RedirectResponse("/login?err=blocked", status_code=303)
    form = await request.form()
    user = str(form.get("user", "")).strip()
    pw = str(form.get("password", ""))
    remember = form.get("remember") == "on"
    if not _pw_ok(user, pw):
        _fails.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1.5)  # brzda proti hádání hesla
        return RedirectResponse("/login?err=1", status_code=303)
    _fails.pop(ip, None)
    resp = RedirectResponse("/", status_code=303)
    days = REMEMBER_DAYS if remember else 0.5
    resp.set_cookie(COOKIE, make_token(user, days), max_age=int(days * 86400) if remember else None,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp

_token: str | None = None
_token_time = 0.0


# ---------- Webshare API ----------
def _xml(text: str) -> ET.Element:
    root = ET.fromstring(text)
    status = root.findtext("status")
    if status != "OK":
        code = root.findtext("code") or "?"
        msg = root.findtext("message") or "neznámá chyba"
        raise HTTPException(502, f"Webshare: {msg} ({code})")
    return root


async def ws_login(client: httpx.AsyncClient) -> str:
    """salt → md5crypt → sha1 → login → token (wst). Token cachujeme ~50 min."""
    global _token, _token_time
    if _token and time.time() - _token_time < 3000:
        return _token
    if not WS_USER or not WS_PASS:
        raise HTTPException(500, "Chybí WS_USER / WS_PASS v .env")

    r = await client.post(f"{WS_API}/salt/", data={"username_or_email": WS_USER})
    salt = _xml(r.text).findtext("salt")
    hashed = hashlib.sha1(md5crypt(WS_PASS, salt).encode()).hexdigest()
    digest = hashlib.md5(f"{WS_USER}:Webshare:{WS_PASS}".encode()).hexdigest()

    r = await client.post(
        f"{WS_API}/login/",
        data={"username_or_email": WS_USER, "password": hashed, "digest": digest, "keep_logged_in": 1},
    )
    _token = _xml(r.text).findtext("token")
    _token_time = time.time()
    return _token


def parse_ident(url: str) -> str | None:
    m = re.search(r"webshare\.cz/(?:#/)?file/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


async def _ws_call(client: httpx.AsyncClient, endpoint: str, data: dict) -> ET.Element:
    """Volání API s tokenem. Když Webshare token mezitím zneplatnil (jiné přihlášení, restart
    na jejich straně…), cache by vracela chybu až 50 min → zahodit token a zkusit jednou znovu."""
    global _token
    for attempt in (1, 2):
        cached = _token is not None             # token z paměti (může být zneplatněný)?
        token = await ws_login(client)
        r = await client.post(f"{WS_API}/{endpoint}/", data={**data, "wst": token})
        try:
            return _xml(r.text)
        except HTTPException:
            # Čerstvé přihlášení + chyba = skutečná chyba (smazaný soubor…) → hned ven, bez opakování
            if attempt == 2 or not cached:
                raise
            _token = None                       # další ws_login() se přihlásí nanovo


# ---------- Fastshare ----------
# Fastshare API nemá. S předplatným a zapnutým „Přímé stahování“ (Můj účet) stránka souboru
# přihlášenému rovnou přesměruje na sNN.fastshare.cloud/download.php → ten aria2 stáhne s cookie.
# Přihlášení = obyčejný formulář; stačí cookie FASTSHARE (platí ~rok). Ukládáme ji do /data,
# ať se po restartu nepřihlašujeme znovu (Fastshare počítá zařízení, max 5).
FS_USER = os.environ.get("FASTSHARE_USER", "")
FS_PASS = os.environ.get("FASTSHARE_PASS", "")
FS_COOKIE_FILE = "/data/fastshare.cookie"
FS_UA = {"User-Agent": "Mozilla/5.0"}


def parse_fastshare(url: str) -> str | None:
    """fastshare.cz/123/nazev.mkv i fastshare.cloud/… → https://fastshare.cloud/123/nazev.mkv
    (název v cestě je nutný, bez něj stránka nepřesměruje)."""
    m = re.search(r"fastshare\.(?:cz|cloud)/(\d+)/([^?#\s]+)", url)
    return f"https://fastshare.cloud/{m.group(1)}/{m.group(2)}" if m else None


def _fs_cookie_load() -> str:
    try:
        with open(FS_COOKIE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


async def fs_login() -> str:
    if not FS_USER or not FS_PASS:
        raise HTTPException(500, "Chybí FASTSHARE_USER / FASTSHARE_PASS v .env")
    async with httpx.AsyncClient(timeout=30, headers=FS_UA, follow_redirects=True) as c:
        await c.post("https://fastshare.cz/sql.php", data={"login": FS_USER, "heslo": FS_PASS})
        cookie = next((ck.value for ck in c.cookies.jar if ck.name == "FASTSHARE"), "")
    if not cookie:
        raise HTTPException(502, "Fastshare: přihlášení se nepovedlo (jméno/heslo v .env?)")
    fd = os.open(FS_COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)                    # i když soubor už existoval s jinými právy
    with os.fdopen(fd, "w") as f:
        f.write(cookie)
    return cookie


async def fs_resolve(page: str) -> tuple[str, str, int, str]:
    """Vrátí (přímý link, název, velikost, cookie). Neplatná cookie i smazaný soubor vrací
    obyčejnou stránku (200) → jednou se přihlásit znovu, pak chyba."""
    from urllib.parse import urlsplit
    cookie = _fs_cookie_load()
    async with httpx.AsyncClient(timeout=30, headers=FS_UA) as c:
        for attempt in (1, 2):
            if not cookie or attempt == 2:
                cookie = await fs_login()
            r = await c.get(page, headers={"Cookie": f"FASTSHARE={cookie}"})
            link = r.headers.get("location", "") if r.is_redirect else ""
            if link:
                break
        else:
            raise HTTPException(502, "Fastshare soubor nedal ke stažení (smazaný? vypršelo předplatné? "
                                     "zapnuté Přímé stahování v Můj účet?)")
        # Cookie posíláme jen na Fastshare, ne na libovolný host z přesměrování
        host = urlsplit(link).hostname or ""
        if not link.startswith("https://") or not host.endswith(".fastshare.cloud"):
            raise HTTPException(502, f"Fastshare přesměroval na neznámý host: {host}")
        r = await c.get(link, headers={"Cookie": f"FASTSHARE={cookie}", "Range": "bytes=0-0"})
        if r.status_code not in (200, 206):
            raise HTTPException(502, f"Fastshare odkaz nejde stáhnout ({r.status_code})")
        m = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
        size = int(m.group(1)) if m else int(r.headers.get("content-length") or 0)
        m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""))
    name = m.group(1).strip() if m else page.rsplit("/", 1)[-1]
    return link, name, size, cookie


async def ws_resolve(client: httpx.AsyncClient, ident: str) -> tuple[str, str, int]:
    """Vrátí (přímý link, název souboru, velikost)."""
    info = await _ws_call(client, "file_info", {"ident": ident})
    name = info.findtext("name") or ident
    size = int(info.findtext("size") or 0)
    link = (await _ws_call(client, "file_link",
                           {"ident": ident, "download_type": "file_download", "force_https": 1})).findtext("link")
    if not link:
        raise HTTPException(502, "Webshare nevrátil odkaz")
    return link, name, size


# ---------- aria2 RPC ----------
async def aria2(client: httpx.AsyncClient, method: str, *params):
    payload = {"jsonrpc": "2.0", "id": "ws", "method": f"aria2.{method}",
               "params": [f"token:{ARIA2_SECRET}", *params]}
    r = await client.post(ARIA2_RPC, json=payload, timeout=10)
    data = r.json()
    if "error" in data:
        raise HTTPException(500, f"aria2: {data['error'].get('message')}")
    return data["result"]


def _fmt_item(d: dict) -> dict:
    total = int(d.get("totalLength", 0))
    done = int(d.get("completedLength", 0))
    speed = int(d.get("downloadSpeed", 0))
    files = d.get("files") or [{}]
    path = files[0].get("path", "")
    name = os.path.basename(path) if path else d.get("gid")
    target = ""
    rel = path[len(DOWNLOAD_ROOT) + 1:] if path.startswith(DOWNLOAD_ROOT + "/") else ""
    rel = rel[len(".incomplete/"):] if rel.startswith(".incomplete/") else rel
    target = os.path.dirname(rel) or rel.split("/")[0]
    eta = int((total - done) / speed) if speed > 0 and total > done else None
    return {
        "gid": d["gid"],
        "name": name,
        "target": target,
        "status": d.get("status"),
        "total": total,
        "done": done,
        "percent": round(done / total * 100, 1) if total else 0,
        "speed": speed,
        "eta": eta,
        "connections": int(d.get("connections", 0)),
        "error": d.get("errorMessage") if d.get("status") == "error" else None,
    }


# ---------- HTTP API ----------
class AddReq(BaseModel):
    url: str
    target: str = "filmy"
    # Z wishlistu: film se uloží jako „Název (Rok) [tmdbid-123].mkv“ → Jellyfin ho pozná napoprvé
    tmdb_id: int | None = None
    title: str | None = None
    year: int | None = None


@app.get("/")
def index():
    # no-cache: po nasazení nové verze prohlížeč nesmí ukazovat starou stránku
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.svg")
def favicon():
    return FileResponse("static/favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}")
def icon(name: str):
    path = _ICONS.get(f"/icons/{name}")
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


# Kdo které stahování přidal (gid → jméno). Na disku, ať po restartu kontejneru rodina dál
# může pauznout/zrušit to svoje. Staré záznamy se čistí, když je aria2 už nezná.
OWNERS_FILE = "/data/owners.json"
try:
    with open(OWNERS_FILE) as _f:
        OWNERS: dict[str, str] = json.load(_f)
except (FileNotFoundError, ValueError):
    OWNERS = {}


def _save_owners() -> None:
    tmp = OWNERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(OWNERS, f)
    os.replace(tmp, OWNERS_FILE)


def _check_owner(request: Request, gid: str) -> None:
    if not is_admin(request) and OWNERS.get(gid) != request.state.user:
        raise HTTPException(403, "Tohle stahování přidal někdo jiný")


@app.get("/api/me")
def me(request: Request):
    return {"user": request.state.user, "admin": is_admin(request), "jf_url": JF_PUBLIC_URL}


@app.post("/api/add")
async def add(req: AddReq, request: Request):
    target = TARGETS.get(req.target)
    if not target:
        raise HTTPException(400, "Neplatný cíl")
    url = req.url.strip()
    async with httpx.AsyncClient(timeout=30) as client:
        ident = parse_ident(url)
        fs_page = parse_fastshare(url)
        opts = {}
        if ident:
            link, name, size = await ws_resolve(client, ident)
        elif fs_page:
            link, name, size, cookie = await fs_resolve(fs_page)
            opts["header"] = [f"Cookie: FASTSHARE={cookie}"]
        elif url.startswith(("http://", "https://")) and not is_admin(request):
            raise HTTPException(400, "Vlož odkaz z Webshare nebo Fastshare")
        elif url.startswith(("http://", "https://")):
            # Fallback: přímý odkaz na veřejný host (test, jiné file hosty).
            # Vnitřní síť (router, HA, Docker…) blokujeme — stahovač nemá být proxy do LAN.
            # aria2 přesměrování následuje a limit na ně nemá → projdeme je tady a aria2 dostane
            # až konečnou, zkontrolovanou URL.
            link, size = await _resolve_direct(client, url)
            name = url.rsplit("/", 1)[-1].split("?")[0] or "download.bin"
        else:
            raise HTTPException(400, "To nevypadá jako odkaz z Webshare, Fastshare ani URL")

        # Archiv potřebuje místo dvakrát: archiv + rozbalený obsah, než se archiv smaže
        archive = extract.is_archive(name)
        await _check_space(client, size * 2 if archive else size)

        # Bezpečný název souboru (žádné lomítka). Kolize v .incomplete řeší aria2 (--auto-file-renaming),
        # kolizi s hotovým souborem v knihovně řeší on_complete.py (přidá „ (2)“).
        name = re.sub(r"[/\\\x00]", "_", name).strip() or "download.bin"
        # Archiv: všechny části do jedné složky, díly roztřídí až extract.py podle názvů uvnitř
        if target == "filmy" and not archive and req.tmdb_id and req.title:
            title = re.sub(r'[\s/\\:*?"<>|]+', " ", re.sub(r"[\x00-\x1f]", "", req.title)).strip()[:150]
            year = f" ({req.year})" if req.year and 1880 <= req.year <= 2100 else ""
            ext = os.path.splitext(name)[1].lower()
            if title and ext in MEDIA_EXT:
                name = f"{title}{year} [tmdbid-{req.tmdb_id}]{ext}"
        if target == "serialy" and not archive:
            name = normalize_episode(name)
        subdir = series_subdir(name) if target == "serialy" and not archive else ""
        # Stahujeme do .incomplete/ (Jellyfin ji nevidí); hook on_complete.py po dokončení
        # přesune soubor do {target}{subdir} a spustí sken té složky v Jellyfinu.
        gid = await aria2(client, "addUri", [link],
                          {"dir": f"{DOWNLOAD_ROOT}/.incomplete/{target}{subdir}", "out": name, **opts})
        if size:
            EXPECTED_SIZE[gid] = size
    OWNERS[gid] = request.state.user
    _save_owners()
    return {"gid": gid, "name": name, "size": size, "target": target + subdir}


SPACE_RESERVE = 10 * 1024**3        # na HDD vždy nechat aspoň 10 GB volných
# gid → velikost z file_info/Content-Length. V paměti: po restartu kontejneru se fronta
# počítá jen podle toho, co už aria2 zná (stejné jako dřív) — přijatelné.
EXPECTED_SIZE: dict[str, int] = {}


async def _check_space(client: httpx.AsyncClient, size: int) -> None:
    """Odmítnout stažení, které se nevejde — počítá i se zbytkem běžících/čekajících stahování."""
    if not size:
        return                          # velikost neznámá (přímá URL bez Content-Length) → nelze ověřit
    pending = 0
    keys = ["gid", "totalLength", "completedLength"]
    for d in await aria2(client, "tellActive", keys) + await aria2(client, "tellWaiting", 0, 100, keys):
        # Ve frontě má aria2 totalLength=0 (velikost zjistí až při startu) → vzít velikost z /api/add
        total = max(int(d.get("totalLength", 0)), EXPECTED_SIZE.get(d.get("gid"), 0))
        pending += max(total - int(d.get("completedLength", 0)), 0)
    free = _free_space() - pending - SPACE_RESERVE
    if size > free:
        gib = lambda b: f"{b / 1024**3:.1f} GB"
        raise HTTPException(507, f"Nevejde se na disk: soubor má {gib(size)}, volné je {gib(max(free, 0))} "
                                 f"(po odečtení rozběhnutých stahování a rezervy {gib(SPACE_RESERVE)})")


async def _resolve_direct(client: httpx.AsyncClient, url: str) -> tuple[str, int]:
    """Projde přesměrování (max 5) a KAŽDÝ host zkontroluje proti vnitřní síti. Vrátí (URL, velikost)."""
    from urllib.parse import urljoin
    for _ in range(6):
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "Přesměrování mimo http/https není povolené")
        await asyncio.to_thread(_reject_private_host, url)
        try:
            r = await client.head(url, follow_redirects=False, timeout=15)
        except httpx.HTTPError as e:
            raise HTTPException(400, f"Odkaz nejde otevřít: {e.__class__.__name__}")
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            continue
        return url, int(r.headers.get("content-length") or 0)
    raise HTTPException(400, "Příliš mnoho přesměrování")


def _reject_private_host(url: str) -> None:
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"Neznámý host: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_global = jen veřejný internet. Pokryje i Tailscale/CGNAT 100.64.0.0/10, které Python
        # nepočítá mezi is_private (Tailscale adresa serveru by jinak prošla).
        if not ip.is_global:
            raise HTTPException(400, "Odkazy do vnitřní sítě nejsou povolené")


# Historie rychlosti pro graf: každé 2 s vzorek aktivních stahování (posledních 10 min).
# V paměti — po restartu kontejneru začíná graf znovu, což nevadí.
HIST_STEP, HIST_LEN = 2, 300
SPEED_HIST: dict[str, deque] = {}


async def _sample_speeds():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                active = await aria2(client, "tellActive", ["gid", "downloadSpeed"])
                now = int(time.time())
                for d in active:
                    SPEED_HIST.setdefault(d["gid"], deque(maxlen=HIST_LEN)).append((now, int(d["downloadSpeed"])))
                live = {d["gid"] for d in active}
                for gid in [g for g in SPEED_HIST if g not in live and SPEED_HIST[g] and now - SPEED_HIST[g][-1][0] > 3600]:
                    del SPEED_HIST[gid]                      # hodinu neaktivní → zahodit
            except Exception:  # noqa: BLE001 — aria2 se zrovna restartuje apod.
                pass
            await asyncio.sleep(HIST_STEP)


# ---------- brzda: když se v Jellyfinu kouká, stahování zpomalí ----------
# Stahování i film jdou přes jeden USB HDD (5400 ot.) → plný zápis 200 MB/s by film sekal.
# Každých 10 s dotaz na Jellyfin /Sessions; kouká-li někdo, aria2 dostane globální limit
# (za běhu, nic se nepřeruší) a /data/playing zdrží rozbalování archivů (extract.py).
# Brzda se uvolní až minutu po konci přehrávání, ať necuká mezi díly. Jellyfin nedostupný = bez brzdy.
THROTTLE_LIMIT = os.environ.get("THROTTLE_LIMIT", "40M")
THROTTLE_STEP, THROTTLE_RELEASE = 10, 60
PLAYBACK: dict = {"on": False, "n": 0, "limit": None}


def _count_playing(sessions: list[dict]) -> int:
    return sum(1 for s in sessions if s.get("NowPlayingItem"))


async def _jf_playing(client: httpx.AsyncClient) -> int | None:
    try:
        r = await client.get(f"{JF_URL}/Sessions", headers=JF_HEADERS, params={"activeWithinSeconds": 120}, timeout=8)
        r.raise_for_status()
        return _count_playing(r.json())
    except Exception:  # noqa: BLE001 — Jellyfin se restartuje apod.
        return None


async def _playback_step(client: httpx.AsyncClient, last_play: float, now: float) -> float:
    n = await _jf_playing(client)
    if n:
        last_play = now
    on = n is not None and (n > 0 or now - last_play < THROTTLE_RELEASE)
    want = THROTTLE_LIMIT if on else "0"
    if PLAYBACK["limit"] != want:
        await aria2(client, "changeGlobalOption", {"max-overall-download-limit": want})
        PLAYBACK["limit"] = want
    PLAYBACK.update(on=on, n=n or 0)
    if on:
        with open(extract.PLAYING_FILE, "w") as f:
            f.write(str(int(now)))
    elif os.path.exists(extract.PLAYING_FILE):
        os.remove(extract.PLAYING_FILE)
    return last_play


async def _playback_guard():
    if not JF_URL:
        return
    last = 0.0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                last = await _playback_step(client, last, time.time())
            except Exception:  # noqa: BLE001 — aria2 se zrovna restartuje; příště znovu
                pass
            await asyncio.sleep(THROTTLE_STEP)


async def _resume_held():
    """Archiv čekal na dokoukání a kontejner se mezitím restartoval → hook už neběží, pustit znovu."""
    for gid, v in extract.get_state().items():
        if v.get("hold") and v.get("state") == "waiting" and v.get("path"):
            asyncio.create_task(asyncio.to_thread(extract.handle, gid, v["path"]))


@app.get("/api/status")
async def status(request: Request):
    async with httpx.AsyncClient() as client:
        active = await aria2(client, "tellActive")
        waiting = await aria2(client, "tellWaiting", 0, 50)
        stopped = await aria2(client, "tellStopped", 0, 50)
        stat = await aria2(client, "getGlobalStat")
    items = [_fmt_item(d) for d in active + waiting + stopped]
    me_ = request.state.user
    for i in items:
        i["hist"] = list(SPEED_HIST.get(i["gid"], ()))
        i["by"] = OWNERS.get(i["gid"], WEB_USER)            # starší stahování bez záznamu = správce
        i["mine"] = i["by"] == me_
    ex = extract.get_state()
    for i in items:
        if i["gid"] in ex:                                  # archiv: čeká na části / rozbaluje / heslo / hotovo
            e = ex[i["gid"]]
            i["extract"] = {"state": e.get("state"), "msg": e.get("msg", ""), "retry": bool(e.get("retry"))}
    known = {i["gid"] for i in items}
    if any(g not in known for g in OWNERS):                 # aria2 je zapomněla → pryč i odsud
        for g in [g for g in OWNERS if g not in known]:
            del OWNERS[g]
        _save_owners()
    if any(g not in known for g in ex):
        await asyncio.to_thread(extract.prune, known)
    return {"items": items, "speed": int(stat.get("downloadSpeed", 0)),
            "free": _free_space(), "logged": bool(WS_USER and WS_PASS),
            "throttle": PLAYBACK["on"], "watching": PLAYBACK["n"]}


INCOMPLETE = os.path.join(DOWNLOAD_ROOT, ".incomplete")


def _discard_partial(path: str) -> int:
    """Smaže rozstažený soubor + řídicí .aria2 — JEN uvnitř .incomplete (knihovny se to nikdy nedotkne).
    Vrátí, kolik místa se reálně uvolnilo (soubor je řídký, velikost ≠ obsazené místo)."""
    real = os.path.realpath(path)
    if not real.startswith(INCOMPLETE + "/"):
        return 0
    freed = 0
    for f in (real, real + ".aria2"):
        try:
            freed += os.stat(f).st_blocks * 512
            os.remove(f)
        except FileNotFoundError:
            pass
    d = os.path.dirname(real)                       # prázdné .incomplete/serialy/Seriál/Season NN
    while d != INCOMPLETE and d.startswith(INCOMPLETE + "/"):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)
    return freed


@app.post("/api/remove/{gid}")
async def remove(gid: str, request: Request):
    """Běžící/čekající/pozastavené: zastavit a smazat rozstažená data.
    Chyba/zrušené: skrýt + smazat zbytek v .incomplete (opakování UI nenabízí, jsou to jen odpadky).
    Hotové: jen skrýt — soubor je v knihovně; kdyby přesun selhal, hotová data se NEmažou."""
    async def quiet(method, *params):              # aria2 zapomíná odebrané pozastavené/čekající → „not found“ nevadí
        try:
            return await aria2(client, method, *params)
        except HTTPException:
            return {}
    _check_owner(request, gid)
    async with httpx.AsyncClient() as client:
        st = await aria2(client, "tellStatus", gid, ["status", "files"])
        status = st.get("status")
        path = ((st.get("files") or [{}])[0]).get("path", "")
        if status in ("active", "waiting", "paused"):
            await aria2(client, "forceRemove", gid)
            # počkat, až aria2 soubor pustí — jinak by po smazání ještě zapsal .aria2
            for _ in range(50):
                if (await quiet("tellStatus", gid, ["status"])).get("status") in ("removed", None):
                    break
                await asyncio.sleep(0.1)
        await quiet("removeDownloadResult", gid)
    # Hotový archiv, který se nerozbalil (chyba/heslo) = jen odpadky v .incomplete → smazat.
    # Čekající na další části / rozbalovaný / rozbalený nechat (rozbalení archiv smaže samo).
    ex_state = extract.get_state().get(gid, {}).get("state")
    if not path or status == "complete" and ex_state not in ("error", "password"):
        return {"ok": True, "freed": 0}
    freed = await asyncio.to_thread(_discard_partial, path)
    return {"ok": True, "freed": freed}


class ExtractReq(BaseModel):
    password: str = ""


_EXTRACT_TASKS: set = set()      # reference na běžící úlohy (jinak je může sebrat GC)


@app.post("/api/extract/{gid}")
async def extract_again(gid: str, req: ExtractReq, request: Request):
    """Zkusit archiv rozbalit znovu — s heslem, nebo po opravě (místo na disku…).
    Běží na pozadí, stav ukazuje /api/status."""
    _check_owner(request, gid)
    st = extract.get_state().get(gid)
    if not st or not st.get("path"):
        raise HTTPException(404, "Tohle není archiv")
    if st.get("state") in ("running", "done"):
        raise HTTPException(409, "Archiv se už rozbaluje" if st["state"] == "running" else "Archiv je už rozbalený")
    if not os.path.realpath(st["path"]).startswith(INCOMPLETE + "/") or not os.path.exists(st["path"]):
        raise HTTPException(410, "Soubor archivu už neexistuje")
    extract.set_state(gid, state="running", msg="Rozbaluji…")
    t = asyncio.create_task(asyncio.to_thread(extract.handle, gid, st["path"], req.password))
    _EXTRACT_TASKS.add(t)
    t.add_done_callback(_EXTRACT_TASKS.discard)
    return {"ok": True}


@app.post("/api/pause/{gid}")
async def pause(gid: str, request: Request):
    _check_owner(request, gid)
    async with httpx.AsyncClient() as client:
        await aria2(client, "pause", gid)
    return {"ok": True}


@app.post("/api/unpause/{gid}")
async def unpause(gid: str, request: Request):
    _check_owner(request, gid)
    async with httpx.AsyncClient() as client:
        await aria2(client, "unpause", gid)
    return {"ok": True}


def _free_space() -> int:
    st = os.statvfs(DOWNLOAD_ROOT)
    return st.f_bavail * st.f_frsize


# ---------- Knihovna (co je stažené) + mazání ----------
def _safe_rel(rel: str) -> str:
    """Povolíme jen cesty uvnitř filmy/ nebo serialy/ — žádné '..', absolutní cesty ani .incomplete.
    Kontroluje i REÁLNOU cestu (realpath), aby symlink uvnitř Media nemohl ukázat ven."""
    rel = rel.strip().lstrip("/")
    norm = os.path.normpath(rel)
    if norm.startswith("..") or os.path.isabs(norm) or norm.split("/")[0] not in TARGETS:
        raise HTTPException(400, "Neplatná cesta")
    real = os.path.realpath(os.path.join(DOWNLOAD_ROOT, norm))
    allowed_root = os.path.realpath(os.path.join(DOWNLOAD_ROOT, norm.split("/")[0]))
    if not real.startswith(allowed_root + "/"):
        raise HTTPException(400, "Neplatná cesta")
    return norm


def _img_ver(it: dict) -> str:
    """Krátká „verze“ obrázků položky z jejich tagů v Jellyfinu. Jde do URL obrázku (?v=…),
    takže po „Opravit rozpoznání“ (nový plakát) prohlížeč nevezme starý z cache."""
    tags = sorted((it.get("ImageTags") or {}).values()) + list(it.get("BackdropImageTags") or [])
    return hashlib.md5("|".join(tags).encode()).hexdigest()[:8] if tags else ""


async def _jf_played_map(client: httpx.AsyncClient, user: str = WEB_USER) -> dict[str, dict]:
    """rel cesta (filmy/..., serialy/...) → {played, name, series, season, episode}. Prázdné, když Jellyfin nedostupný."""
    if not (JF_URL and JF_TOKEN):
        return {}
    try:
        users = (await client.get(f"{JF_URL}/Users", headers=JF_HEADERS, timeout=10)).json()
        uid = next((u["Id"] for u in users if u.get("Name") == user),
                   next((u["Id"] for u in users if u.get("Name") == WEB_USER), users[0]["Id"] if users else None))
        if not uid:
            return {}
        r = await client.get(
            f"{JF_URL}/Items",
            params={"userId": uid, "Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,Overview,Genres,ProviderIds", "EnableImageTypes": "Primary,Backdrop,Logo",
                    "ImageTypeLimit": 1},
            headers=JF_HEADERS, timeout=15)
        out = {}
        for it in r.json().get("Items", []):
            path = it.get("Path") or ""
            ud = it.get("UserData") or {}
            imgs = it.get("ImageTags") or {}
            if path.startswith(JF_MEDIA):
                out[path[len(JF_MEDIA):]] = {
                    # rozkoukáno = pozice > 0 a ještě není zhlédnuto (pro „Pokračovat ve sledování“)
                    "progress": round(ud.get("PlayedPercentage") or 0, 1) if ud.get("PlaybackPositionTicks") else 0,
                    "last_played": ud.get("LastPlayedDate"),
                    "has_img": "Primary" in imgs,                       # u dílu = náhled 16:9
                    "has_backdrop": bool(it.get("BackdropImageTags")),
                    "has_logo": "Logo" in imgs,
                    "matched": bool(it.get("ProviderIds")),         # spárováno s TMDB/IMDb/TVDB
                    "img_v": _img_ver(it),
                    "jf_id": it.get("Id"),
                    "series_id": it.get("SeriesId"),
                    "played": bool((it.get("UserData") or {}).get("Played")),
                    "title": it.get("Name"),
                    "series": it.get("SeriesName"),
                    "season": it.get("ParentIndexNumber"),
                    "episode": it.get("IndexNumber"),
                    "year": it.get("ProductionYear"),
                    "overview": it.get("Overview"),
                    "minutes": round(it["RunTimeTicks"] / 600_000_000) if it.get("RunTimeTicks") else None,
                    "rating": it.get("CommunityRating"),
                    "genres": it.get("Genres") or [],
                }
        return out
    except Exception:  # noqa: BLE001
        return {}


async def _jf_series_info(client: httpx.AsyncClient) -> dict[str, dict]:
    """SeriesId → popis seriálu (pro hlavičku stránky seriálu)."""
    if not (JF_URL and JF_TOKEN):
        return {}
    try:
        r = await client.get(f"{JF_URL}/Items",
                             params={"Recursive": "true", "IncludeItemTypes": "Series",
                                     "Fields": "Overview,Genres,ProviderIds", "EnableImageTypes": "Primary,Backdrop,Logo",
                                     "ImageTypeLimit": 1},
                             headers=JF_HEADERS, timeout=15)
        return {s["Id"]: {"name": s.get("Name"), "overview": s.get("Overview"), "year": s.get("ProductionYear"),
                          "end": (s.get("EndDate") or "")[:4] or None, "status": s.get("Status"),
                          "rating": s.get("CommunityRating"), "genres": s.get("Genres") or [],
                          "has_backdrop": bool(s.get("BackdropImageTags")),
                          "has_logo": "Logo" in (s.get("ImageTags") or {}),
                          "matched": bool(s.get("ProviderIds")),
                          "img_v": _img_ver(s)}
                for s in r.json().get("Items", [])}
    except Exception:  # noqa: BLE001
        return {}


def _scan_library() -> tuple[list[tuple[str, str, str, str, os.stat_result]], int]:
    """Projde filmy/ a serialy/ na disku. Synchronní → volat přes asyncio.to_thread (HDD je pomalý)."""
    found, used = [], 0
    for target in TARGETS:
        root = os.path.join(DOWNLOAD_ROOT, target)
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                    continue
                st = os.stat(os.path.join(dirpath, f))
                used += st.st_size
                found.append((target, root, dirpath, f, st))
    return found, used


@app.get("/api/library")
async def library(request: Request):
    async with httpx.AsyncClient() as client:
        (played, series), (found, used) = await asyncio.gather(
            asyncio.gather(_jf_played_map(client, request.state.user), _jf_series_info(client)),
            asyncio.to_thread(_scan_library))
    items = []
    for target, root, dirpath, f, st in found:
        rel = os.path.relpath(os.path.join(dirpath, f), DOWNLOAD_ROOT)
        meta = played.get(rel, {})
        items.append({
            "rel": rel, "target": target, "name": f, "size": st.st_size,
            "mtime": int(st.st_mtime),
            # přidáno = kdy soubor dorazil na disk (on_complete ho přesouvá → ctime); mtime u kopírovaných
            # souborů zůstává původní (třeba rok 2018)
            "added": int(st.st_ctime),
            "group": os.path.relpath(dirpath, root) if dirpath != root else "",
            "played": meta.get("played"), "title": meta.get("title"),
            "series": meta.get("series"), "season": meta.get("season"),
            "episode": meta.get("episode"), "year": meta.get("year"),
            "jf_id": meta.get("jf_id"), "series_id": meta.get("series_id"),
            "overview": meta.get("overview"), "minutes": meta.get("minutes"),
            "rating": meta.get("rating"), "genres": meta.get("genres", []),
            "progress": meta.get("progress", 0), "last_played": meta.get("last_played"),
            "has_img": meta.get("has_img", False), "has_backdrop": meta.get("has_backdrop", False),
            "has_logo": meta.get("has_logo", False),
            "matched": meta.get("matched"),
            "img_v": meta.get("img_v"),
        })
    items.sort(key=lambda i: (i["target"], i["group"], i["name"]))
    return {"items": items, "series": series, "used": used, "free": _free_space()}


async def _jf_refresh_library(client: httpx.AsyncClient, target: str) -> None:
    if not (JF_URL and JF_TOKEN):
        return
    try:
        libs = (await client.get(f"{JF_URL}/Library/VirtualFolders", headers=JF_HEADERS, timeout=10)).json()
        lib = next((l for l in libs if f"{JF_MEDIA}{target}" in (l.get("Locations") or [])), None)
        if lib:
            await client.post(f"{JF_URL}/Items/{lib['ItemId']}/Refresh", params={"Recursive": "true"},
                              headers=JF_HEADERS, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _prune_empty_dirs(d: str, root: str) -> None:
    """Uklidit prázdné složky směrem nahoru (série → seriál / složka filmu), kořen filmy/serialy nikdy."""
    while d != root and d.startswith(root + "/"):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def _sidecars(d: str, video: str) -> list[str]:
    """Titulky/pomocné soubory patřící k videu (Film.cs.srt, Film.nfo…) — NIKDY jiné video.
    Pozor na Rocky.mkv vs. Rocky.II.mkv: soubor Rocky.II.cs.srt patří k Rocky.II, ne k Rocky
    → přeskočit, když ho „pokrývá“ delší základ jiného videa ve stejné složce."""
    base = os.path.splitext(video)[0]
    others = [os.path.splitext(f)[0] for f in os.listdir(d)
              if f != video and os.path.splitext(f)[1].lower() in MEDIA_EXT]
    out = []
    for f in os.listdir(d):
        if (f != video and f.startswith(base + ".")
                and os.path.splitext(f)[1].lower() not in MEDIA_EXT
                and not any(len(o) > len(base) and f.startswith(o + ".") for o in others)
                and os.path.isfile(os.path.join(d, f))):
            out.append(f)
    return out


def _delete_file(full: str, root: str) -> int:
    size = os.path.getsize(full)
    d, video = os.path.split(full)
    for f in _sidecars(d, video):          # vybrat dřív, než video zmizí ze seznamu
        os.remove(os.path.join(d, f))
    os.remove(full)
    _prune_empty_dirs(d, root)
    return size


@app.delete("/api/library")
async def library_delete(rel: str, request: Request):
    require_admin(request)
    rel = _safe_rel(rel)
    full = os.path.join(DOWNLOAD_ROOT, rel)
    if not os.path.isfile(full) or os.path.splitext(full)[1].lower() not in MEDIA_EXT:
        raise HTTPException(404, "Soubor nenalezen")
    target = rel.split("/")[0]
    size = await asyncio.to_thread(_delete_file, full, os.path.join(DOWNLOAD_ROOT, target))
    async with httpx.AsyncClient() as client:
        await _jf_refresh_library(client, target)
    return {"ok": True, "freed": size}


def _delete_dir(full: str, root: str) -> int:
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(full) for f in fs)
    shutil.rmtree(full)
    _prune_empty_dirs(os.path.dirname(full), root)      # prázdný seriál po smazání poslední série
    return size


@app.delete("/api/library/dir")
async def library_delete_dir(rel: str, request: Request):
    """Smaže celou složku seriálu nebo série (serialy/Seriál[/Season NN]) nebo složku filmu.
    Kořen filmy/serialy smazat nejde (min. 2 úrovně)."""
    require_admin(request)
    rel = _safe_rel(rel)
    full = os.path.join(DOWNLOAD_ROOT, rel)
    if len(rel.split("/")) < 2 or not os.path.isdir(full) or os.path.islink(full):
        raise HTTPException(400, "Neplatná složka")
    target = rel.split("/")[0]
    size = await asyncio.to_thread(_delete_dir, full, os.path.join(DOWNLOAD_ROOT, target))
    async with httpx.AsyncClient() as client:
        await _jf_refresh_library(client, target)
    return {"ok": True, "freed": size}


# Plakáty z Jellyfinu (prohlížeč na Jellyfin nevidí / nemá token → proxy přes nás, za loginem)
_IMG_ID = re.compile(r"^[0-9a-f]{32}$")
# Typ obrázku → parametry pro Jellyfin. Pevná sada (žádné libovolné rozměry z URL).
_IMG_TYPES = {
    "Primary": {"fillHeight": 450, "quality": 85},      # plakát (u dílu náhled 16:9)
    "Thumb": {"maxWidth": 640, "quality": 80},          # náhled dílu v seznamu
    "Backdrop": {"maxWidth": 1920, "quality": 80},      # pozadí hero sekce
    "Logo": {"maxWidth": 800},                          # průhledné PNG s názvem
}


@app.get("/api/img/{item_id}")
async def image(item_id: str, type: str = "Primary"):
    if not _IMG_ID.match(item_id) or type not in _IMG_TYPES or not (JF_URL and JF_TOKEN):
        raise HTTPException(404)
    # Thumb = Primary obrázek dílu, jen menší (Jellyfin má u dílů snímek z epizody jako Primary)
    jf_type = "Primary" if type == "Thumb" else type
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{JF_URL}/Items/{item_id}/Images/{jf_type}",
                             params=_IMG_TYPES[type], headers=JF_HEADERS, timeout=15)
    if r.status_code != 200:
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=604800"})   # URL nese ?v= verzi obrázku


# ---------- Oprava rozpoznání (Jellyfin „Identifikovat“) ----------
# Výsledky hledání držíme na serveru a klient posílá jen index → do Jellyfinu nikdy nejde
# libovolné tělo z prohlížeče. Klíč = id položky; stačí posledních pár hledání.
_IDENT: dict[str, list[dict]] = {}


class IdentSearch(BaseModel):
    id: str
    kind: str = "movie"          # movie | series
    name: str
    year: int | None = None


class IdentApply(BaseModel):
    id: str
    index: int


def _small_img(url: str | None) -> str | None:
    """TMDB vrací plakát v plném rozlišení (MB) → pro výběr stačí w342."""
    if url and url.startswith("https://image.tmdb.org/t/p/original/"):
        return url.replace("/t/p/original/", "/t/p/w342/", 1)
    return url if url and url.startswith("https://") else None


@app.post("/api/identify/search")
async def identify_search(req: IdentSearch, request: Request):
    require_admin(request)
    if not _IMG_ID.match(req.id) or req.kind not in ("movie", "series") or not req.name.strip():
        raise HTTPException(400, "Neplatný dotaz")
    if not (JF_URL and JF_TOKEN):
        raise HTTPException(503, "Jellyfin není nastavený")
    info = {"Name": req.name.strip()[:200]}
    if req.year and 1880 <= req.year <= 2100:
        info["Year"] = req.year
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{JF_URL}/Items/RemoteSearch/{'Movie' if req.kind == 'movie' else 'Series'}",
                              headers=JF_HEADERS, json={"SearchInfo": info, "ItemId": req.id}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, f"Jellyfin hledání selhalo (HTTP {r.status_code})")
    res = r.json()[:12]
    if len(_IDENT) > 50:
        _IDENT.clear()
    _IDENT[req.id] = res
    return [{"index": k, "name": x.get("Name"), "year": x.get("ProductionYear"),
             "overview": x.get("Overview"), "image": _small_img(x.get("ImageUrl")),
             "tmdb": (x.get("ProviderIds") or {}).get("Tmdb"), "imdb": (x.get("ProviderIds") or {}).get("Imdb"),
             "source": x.get("SearchProviderName")} for k, x in enumerate(res)]


@app.post("/api/identify/apply")
async def identify_apply(req: IdentApply, request: Request):
    require_admin(request)
    res = _IDENT.get(req.id) or []
    if not _IMG_ID.match(req.id) or not (0 <= req.index < len(res)):
        raise HTTPException(400, "Výsledek hledání vypršel, hledej znovu")
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{JF_URL}/Items/RemoteSearch/Apply/{req.id}", params={"ReplaceAllImages": "true"},
                              headers=JF_HEADERS, json=res[req.index], timeout=60)
    if r.status_code not in (200, 204):
        raise HTTPException(502, f"Jellyfin nepřijal výběr (HTTP {r.status_code})")
    _IDENT.pop(req.id, None)
    return {"ok": True}


# ---------- Objevit + wishlist (TMDB → Webshare) ----------
# Logika (kvalita/jazyk z názvu souboru, pořadí, úložiště) je v discover.py, tady jen HTTP.
_JF_TMDB: tuple[float, dict[str, set[str]]] = (0.0, {})


async def _jf_tmdb_ids(client: httpx.AsyncClient) -> dict[str, set[str]]:
    """TMDB id filmů a seriálů, které už jsou v Jellyfinu → „V knihovně“. Cache 60 s."""
    global _JF_TMDB
    if time.time() - _JF_TMDB[0] < 60:
        return _JF_TMDB[1]
    out: dict[str, set[str]] = {"movie": set(), "tv": set()}
    if JF_URL and JF_TOKEN:
        try:
            r = await client.get(f"{JF_URL}/Items", headers=JF_HEADERS, timeout=10,
                                 params={"Recursive": "true", "IncludeItemTypes": "Movie,Series", "Fields": "ProviderIds"})
            for it in r.json().get("Items", []):
                if tid := (it.get("ProviderIds") or {}).get("Tmdb"):
                    out["movie" if it.get("Type") == "Movie" else "tv"].add(str(tid))
        except Exception:  # noqa: BLE001 — Jellyfin dočasně nedostupný → jen chybí štítek
            return _JF_TMDB[1]
    _JF_TMDB = (time.time(), out)
    return out


def _check_kind(kind: str) -> None:
    if kind not in discover.KINDS:
        raise HTTPException(400, "Neplatný druh (movie|tv)")


async def _tmdb(client: httpx.AsyncClient, path: str, **kw):
    try:
        return await discover.tmdb(client, path, **kw)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None
    except httpx.HTTPError:
        raise HTTPException(502, "TMDB neodpovídá") from None


def _wish(user: str) -> list[dict]:
    return discover.WISH.setdefault(user, [])


async def _mark(client: httpx.AsyncClient, user: str, cards: list[dict]) -> list[dict]:
    have = await _jf_tmdb_ids(client)
    wished = {discover.key(w["kind"], w["id"]) for w in _wish(user)}
    for c in cards:
        c["have"] = str(c["id"]) in have[c["kind"]]
        c["wish"] = discover.key(c["kind"], c["id"]) in wished
    return cards


@app.get("/api/tmdb/genres")
async def tmdb_genres(kind: str = "movie"):
    _check_kind(kind)
    async with httpx.AsyncClient() as client:
        d = await _tmdb(client, f"/genre/{kind}/list", ttl=86400)
    return d.get("genres", [])


@app.get("/api/discover")
async def discover_list(request: Request, kind: str = "movie", lst: str = Query("trending", alias="list"),
                        genre: str = "", min_rating: float = Query(0, alias="min"), page: int = 1):
    _check_kind(kind)
    if lst not in ("trending", "new", "top", "popular") or not re.fullmatch(r"\d{0,6}", genre) \
            or not (0 <= min_rating <= 10) or not (1 <= page <= 50):
        raise HTTPException(400, "Neplatný dotaz")
    path, params = discover.discover_query(kind, lst, genre, min_rating, page)
    async with httpx.AsyncClient() as client:
        d = await _tmdb(client, path, **params)
        cards = [discover.card(kind, x) for x in d.get("results", [])]
        if path.startswith("/trending"):
            cards = [c for c in cards if c["votes"] >= 20 or c["rating"]]   # trendy bez hodnocení = šum
        cards = await _mark(client, request.state.user, cards)
    return {"items": cards, "page": d.get("page", page), "pages": min(d.get("total_pages", 1), 50)}


@app.get("/api/tmdb/search")
async def tmdb_search(request: Request, q: str, kind: str = "movie"):
    _check_kind(kind)
    q = q.strip()[:100]
    if not q:
        return []
    async with httpx.AsyncClient() as client:
        d = await _tmdb(client, f"/search/{kind}", query=q, ttl=600)
        return await _mark(client, request.state.user, [discover.card(kind, x) for x in d.get("results", [])[:20]])


async def _details(client: httpx.AsyncClient, kind: str, tmdb_id: int) -> dict:
    d = await _tmdb(client, f"/{kind}/{tmdb_id}", ttl=6 * 3600)
    c = discover.card(kind, d)
    c["genres"] = [g["name"] for g in d.get("genres") or []]
    c["minutes"] = d.get("runtime") or (d.get("episode_run_time") or [None])[0]
    c["seasons"] = d.get("number_of_seasons")
    if kind == "tv":                                    # {„11“: 14} – kolik dílů má série (bez speciálů)
        c["season_eps"] = {str(x["season_number"]): x.get("episode_count") or 0
                           for x in d.get("seasons") or [] if x.get("season_number")}
    return c


async def ws_search(client: httpx.AsyncClient, what: str, limit: int = 60, sort: str = "",
                    category: str = "video") -> list[dict]:
    root = await _ws_call(client, "search", {"what": what, "category": category, "sort": sort, "limit": limit, "offset": 0})
    out = []
    for f in root.findall("file"):
        try:
            out.append({"ident": f.findtext("ident"), "name": f.findtext("name") or "",
                        "size": int(f.findtext("size") or 0),
                        "positive_votes": int(f.findtext("positive_votes") or 0),
                        "negative_votes": int(f.findtext("negative_votes") or 0),
                        "password": f.findtext("password") == "1"})
        except ValueError:
            continue
    return out


async def _find_files(client: httpx.AsyncClient, kind: str, tmdb_id: int,
                      season: int | None = None, ep: int | None = None) -> tuple[dict, list[dict]]:
    """Hledá na Webshare podle českého i originálního názvu (+ „2160p“, ať 4K nezapadne mezi
    desítkami 1080p) → jen soubory k tomuhle titulu, seřazené od nejlepšího.
    Filmy navíc „název 1“ (první díly série bývají „Iron Man 1 (2008)“) a název seřazený od největších:
    u „iron man“ zabere relevanci 60+ dílů kresleného seriálu a film se do výsledků nedostane."""
    info = await _details(client, kind, tmdb_id)
    titles = [t for t in dict.fromkeys([info["title"], info["orig"]]) if t]
    main = info["orig"] or info["title"]
    if kind == "movie":
        queries = [(t, "") for t in titles] + [(f"{main} 2160p", ""), (f"{main} 1", ""), (main, "largest")]
    else:
        queries = [(t, "") for t in titles]
        # Hledání jen podle názvu vrátí 100 souborů z celého seriálu → konkrétní série/díl cíleně
        # („south park s11“, „south park s01e04“ – Webshare pak vrací právě je)
        if season is not None:
            queries = [(f"{t} s{season:02d}" + (f"e{ep:02d}" if ep else ""), "") for t in titles]
    found = await asyncio.gather(*(ws_search(client, discover.fold(q), 100 if kind == "tv" else 60, sort)
                                   for q, sort in queries))
    files = {f["ident"]: f for batch in found for f in batch}
    ranked = discover.rank([c for f in files.values() if (c := discover.classify(f, kind, titles, info["year"]))])
    if season is None:                                  # souhrn pro wishlist jen z celkového hledání
        discover.record_avail(discover.key(kind, tmdb_id), ranked)
    return info, ranked


async def _find_packs(client: httpx.AsyncClient, info: dict, season: int | None) -> list[dict]:
    """Archivy s celými sériemi (Webshare je má v kategorii „archives“, hledání videí je nevrátí)."""
    titles = [t for t in dict.fromkeys([info["title"], info["orig"]]) if t]
    queries = titles + ([f"{t} {season} serie" for t in titles] if season else [])
    found = await asyncio.gather(*(ws_search(client, discover.fold(q), 100, "", "archives") for q in queries))
    files = {f["ident"]: f for batch in found for f in batch}
    packs = [c for f in files.values() if (c := discover.classify_pack(f, titles))]
    if season:
        packs = [x for x in packs if x["pack"] == season]
    return sorted(discover.rank(packs), key=lambda x: x["pack"] or 999)


async def _jf_episodes(client: httpx.AsyncClient, tmdb_id: int) -> list[list[int]]:
    """Díly seriálu (TMDB id), které už jsou v Jellyfinu: [[série, díl], …]. Výpadek Jellyfinu → []."""
    if not (JF_URL and JF_TOKEN):
        return []
    have: set[tuple[int, int]] = set()
    try:
        r = await client.get(f"{JF_URL}/Items", headers=JF_HEADERS, timeout=10,
                             params={"Recursive": "true", "IncludeItemTypes": "Series", "Fields": "ProviderIds"})
        for sid in [it["Id"] for it in r.json().get("Items", [])
                    if str((it.get("ProviderIds") or {}).get("Tmdb")) == str(tmdb_id)]:
            r = await client.get(f"{JF_URL}/Shows/{sid}/Episodes", headers=JF_HEADERS, timeout=10)
            for e in r.json().get("Items", []):
                s, n = e.get("ParentIndexNumber"), e.get("IndexNumber")
                if s is None or n is None:
                    continue
                for k in range(n, (e.get("IndexNumberEnd") or n) + 1):    # dvojdíl S01E01-E02
                    have.add((s, k))
    except Exception:  # noqa: BLE001
        return []
    return sorted([s, e] for s, e in have)


@app.get("/api/tmdb/{kind}/{tmdb_id}")
async def tmdb_detail(kind: str, tmdb_id: int, request: Request):
    _check_kind(kind)
    async with httpx.AsyncClient() as client:
        c = (await _mark(client, request.state.user, [await _details(client, kind, tmdb_id)]))[0]
    c["avail"] = discover.AVAIL.get(discover.key(kind, tmdb_id))
    return c


@app.get("/api/wishlist")
async def wishlist(request: Request):
    async with httpx.AsyncClient() as client:
        have = await _jf_tmdb_ids(client)
    out = []
    for w in _wish(request.state.user):
        av = discover.AVAIL.get(discover.key(w["kind"], w["id"]), {})
        out.append({**w, "have": str(w["id"]) in have[w["kind"]], "wish": True, "avail": av or None,
                    "new_4k": discover.new_4k(w, av)})
    return out


_BG: set[asyncio.Task] = set()


class WishReq(BaseModel):
    kind: str
    id: int


@app.post("/api/wishlist")
async def wishlist_add(req: WishReq, request: Request):
    _check_kind(req.kind)
    items = _wish(request.state.user)
    if any(w["kind"] == req.kind and w["id"] == req.id for w in items):
        return {"ok": True}
    if len(items) >= 500:
        raise HTTPException(400, "Wishlist je plný (500)")
    async with httpx.AsyncClient() as client:
        c = await _details(client, req.kind, req.id)       # údaje z TMDB, ne z prohlížeče
    items.insert(0, {k: c[k] for k in ("kind", "id", "title", "orig", "year", "rating", "poster", "backdrop",
                                       "overview", "genres")} | {"added": int(time.time())})
    discover.save_wish()
    # dostupnost na Webshare hned, ne až za 6 h (odkaz v _BG, jinak by task mohl sebrat GC)
    t = asyncio.create_task(_check_one(req.kind, req.id))
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return {"ok": True}


@app.delete("/api/wishlist/{kind}/{tmdb_id}")
def wishlist_del(kind: str, tmdb_id: int, request: Request):
    items = _wish(request.state.user)
    items[:] = [w for w in items if not (w["kind"] == kind and w["id"] == tmdb_id)]
    discover.save_wish()
    return {"ok": True}


@app.get("/api/wishlist/files")
async def wishlist_files(kind: str, id: int, request: Request,
                         season: int | None = Query(None, ge=0, le=99), ep: int | None = Query(None, ge=1, le=999)):
    """Verze na Webshare (živé hledání). Otevření = uživatel štítek „nově ve 4K“ viděl.
    Seriál: season = cílené hledání série (+ archivy celé série), season+ep = jeden díl."""
    _check_kind(kind)
    if kind != "tv":
        season = ep = None
    elif ep and season is None:
        raise HTTPException(400, "U dílu chybí číslo série")
    packs, have = [], []
    async with httpx.AsyncClient(timeout=30) as client:
        info, ranked = await _find_files(client, kind, id, season, ep)
        if kind == "tv":
            packs, have = await asyncio.gather(
                _find_packs(client, info, season) if not ep else asyncio.sleep(0, []), _jf_episodes(client, id))
    if season is not None:
        ranked = [x for x in ranked if x["ep"] and x["ep"][0] == season and (not ep or x["ep"][1] == ep)]
    for w in _wish(request.state.user):
        if w["kind"] == kind and w["id"] == id and not w.get("seen_4k") and discover.AVAIL.get(
                discover.key(kind, id), {}).get("has_4k_cz"):
            w["seen_4k"] = True
            discover.save_wish()
    if kind == "tv":                                    # seriál: po dílech, u každého nejlepší verze první
        ranked = sorted(ranked, key=lambda x: x["ep"] or [999, 999])
    return {"info": info, "files": ranked[:400 if kind == "tv" else 150], "packs": packs, "have": have,
            "summary": discover.AVAIL.get(discover.key(kind, id))}


async def _check_one(kind: str, tmdb_id: int) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await _find_files(client, kind, tmdb_id)
    except Exception as e:  # noqa: BLE001 — Webshare/TMDB výpadek: zkusí se příště
        print(f"[wish] {kind}:{tmdb_id} kontrola selhala: {e}", flush=True)


async def _wish_watch():
    """Hlídač: co 10 min projde wishlisty všech, položky starší než WATCH_EVERY znovu prohledá.
    Mezi hledáními pauza, ať Webshare nedostane desítky dotazů naráz."""
    await asyncio.sleep(120)
    while True:
        try:
            async with httpx.AsyncClient() as client:
                have = await _jf_tmdb_ids(client)
            due = {(w["kind"], w["id"]) for items in discover.WISH.values() for w in items
                   if str(w["id"]) not in have[w["kind"]]
                   and time.time() - discover.AVAIL.get(discover.key(w["kind"], w["id"]), {}).get("checked", 0)
                   > discover.WATCH_EVERY}
            for kind, tmdb_id in due:
                await _check_one(kind, tmdb_id)
                await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001
            print(f"[wish] hlídač: {e}", flush=True)
        await asyncio.sleep(600)
