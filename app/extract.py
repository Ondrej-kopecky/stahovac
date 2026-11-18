"""
Rozbalování archivů (.rar i po částech, .zip, .7z) po stažení — volá ho on_complete.py
(aria2 hook) a /api/extract/{gid} (heslo, zkusit znovu).

Tok: hotový díl archivu zůstane v .incomplete → když jsou všechny části (žádná nemá .aria2
a 7z nehlásí chybějící svazek) → výpis a kontrola (cesty ven, symlinky, místo) → 7z x do
.incomplete/.extract/… → videa + titulky roztřídit (seriály podle SxxEyy) → smazat archiv →
sken Jellyfinu. Stav po gid v /data/extract.json, UI ho ukazuje u stahování.

Funkce na názvy seriálů jsou tady (ne v app.py), protože hook běží jako samostatný skript.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

DOWNLOAD_ROOT = "/downloads"
INCOMPLETE = DOWNLOAD_ROOT + "/.incomplete"
STATE_FILE = "/data/extract.json"
LOCK_DIR = "/data/locks"
SPACE_RESERVE = 10 * 1024**3
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
SAMPLE_MAX = 300 * 1024**2            # „sample“ video menší než tohle = ukázka, nechceme ji
# Když se v Jellyfinu kouká, rozbalování počká (7z na USB HDD by film sekal).
# Značku obnovuje app.py každých ~10 s; starší než PLAYING_FRESH = app neběží → nečekat.
PLAYING_FILE = "/data/playing"
PLAYING_FRESH = 90
HOLD_STEP = 30
JF_URL = os.environ.get("JELLYFIN_URL", "")
JF_TOKEN = os.environ.get("JELLYFIN_TOKEN", "")
# Pod jakou cestou vidí Jellyfin složku s filmy (u něj v kontejneru/LXC), výchozí /media.
# Stahovač má stejnou složku jako /downloads → filmy/… = JF_MEDIA_PATH/filmy/…
JF_MEDIA_PATH = os.environ.get("JELLYFIN_MEDIA_PATH", "/media").rstrip("/")


def log(msg):
    print(f"[extract] {msg}", flush=True)


# ---------- názvy ----------
def normalize_episode(name: str) -> str:
    """Fastshare píše díly jako 'Futurama_08E05_Název.mkv' (bez S) → 'Futurama S08E05 Název.mkv',
    jinak by je series_subdir ani Jellyfin nepoznal. Stejně 'South Park 13x01.mp4' → 'South Park S13E01.mp4'
    (6. 11. 2026, archiv 13. série skončil v kořeni serialy/)."""
    m = re.match(r"^(?P<show>.+?)[ ._-]+(?P<s>\d{1,2})[Ex](?P<e>\d{1,3})(?P<rest>[ ._-].*|\.[^.]+)$", name, re.IGNORECASE)
    if not m:
        return name
    show = re.sub(r"[._]+", " ", m.group("show")).strip(" -")
    rest = m.group("rest")
    rest = rest if rest.startswith(".") else " " + rest.lstrip(" ._-")
    return f"{show} S{int(m.group('s')):02d}E{int(m.group('e')):02d}{rest}"


def series_subdir(name: str) -> str:
    """
    Jellyfin u seriálů vyžaduje 'Seriál/Season NN/soubor'. Z názvu typu
    'Futurama S07E10 Near-Death Wish 1080p...' vyrobí '/Futurama/Season 07'.
    Když vzor SxxEyy nenajde, nechá soubor v kořeni (a Jellyfin ho pak neuvidí — lepší než hádat).
    """
    m = re.match(r"^(?P<show>.+?)[ ._-]+S(?P<s>\d{1,2})E\d{1,3}", name, re.IGNORECASE)
    if not m:
        return ""
    show = re.sub(r"[._]+", " ", m.group("show")).strip(" -")
    show = re.sub(r"\s+", " ", show)
    show = library_dir(drop_season_suffix(show, int(m.group("s"))))
    return f"/{show}/Season {int(m.group('s')):02d}"


def library_name(name: str, sub: str) -> str:
    """Název dílu sladěný se složkou seriálu, když jde o stejný název jinak napsaný:
    'South park S18E01 - X.mkv' do '/South Park/Season 18' → 'South Park S18E01 - X.mkv'.
    Jiný název ('Cerveny-trpaslik S12E01' v Red Dwarf) nechá být."""
    if not sub or not (m := re.match(r"^(?P<show>.+?)(?P<rest>[ ._-]+S\d{1,2}E\d{1,3}.*)$", name, re.I)):
        return name
    folder = sub.split("/")[1]
    if m["show"] != folder and _fold(m["show"]) == _fold(folder):
        return folder + " " + m["rest"].lstrip(" ._-")
    return name


def drop_season_suffix(show: str, season: int) -> str:
    """'South Park 06 S06E01' → 'South Park' (uploader dal číslo série do názvu seriálu, Jellyfin by
    založil seriál „South Park 06“). Jen když číslo = série a je s nulou, nebo seriál bez čísla už
    v knihovně je — 'Babylon 5 S05E01' zůstane."""
    m = re.match(r"^(?P<base>.+?)[ ._-]+(?P<n>\d{1,2})$", show)
    if not m or int(m.group("n")) != season:
        return show
    if m.group("n").startswith("0") or os.path.isdir(os.path.join(DOWNLOAD_ROOT, "serialy", m.group("base"))):
        return m.group("base")
    return show


# Složka jen se sérií („05“, „Season 5“, „S05“, „5. série“) — název seriálu z ní nevyčteme
SEASON_DIR = re.compile(r"^(?:(?:season|série|serie|sez[oó]na|řada|s)[ ._-]*)?\d{1,2}(?:\.?[ ._-]*(?:série|serie|sez[oó]na|řada|season))?$", re.I)
_SHOW_JUNK = re.compile(
    r"\b(?:S\d{1,2}(?:[ ._-]*-[ ._-]*S?\d{1,2})?|\d{1,2}[ ._]*-[ ._]*\d{1,2}|komplet\w*|complete|season\w*|série|serie|sez[oó]n[ay]|řady?"
    r"|cz|sk|en|dabing|titulky|bez[ ._-]*hesla|\d{3,4}p|hd|fullhd|web-?rip|web-?dl|blu-?ray|x26[45]|h\.?26[45]|(?:19|20)\d{2})\b", re.I)


def show_name(text: str) -> str:
    """Název seriálu z názvu archivu/složky: 'Red Dwarf - Červený trpaslík 1-12 komplet' → 'Red Dwarf',
    'Futurama.S01-S07.1080p.CZ' → 'Futurama'. Před „ - “ bývá originální název."""
    text = re.sub(r"(?:\.part\d+)?\.(?:rar|zip|7z)(?:\.\d{3})?$", "", text, flags=re.I)
    text = text.split(" - ")[0]
    # Název bez mezer ('South-park-S16-CZ-HD-720p-16.Série_Uploader') → pomlčka je oddělovač jako tečka
    text = re.sub(r"[._-]+" if " " not in text else r"[._]+", " ", text)
    # Seriál končí tam, kde začíná Sxx / SxxEyy (za ním bývá jen kvalita, jazyk a podpis uploadera)
    if (m := re.search(r"\bS\d{1,2}(?:E\d{1,3})?\b", text, re.I)) and text[:m.start()].strip(" -"):
        text = text[:m.start()]
    text = _SHOW_JUNK.sub(" ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)          # po odstranění zbylé prázdné závorky
    return re.sub(r"\s+", " ", text).strip(" -")


_SEASON_RANGE = re.compile(r"\b\d{1,2}[ ._]*-[ ._]*\d{1,2}\b|\bS\d{1,2}[ ._]*-[ ._]*S?\d{1,2}\b", re.I)
_SEASON_NO = re.compile(r"(?<![\d.])(\d{1,2})\.?[ ._-]*(?:série|serie|sez[oó]na|řada)\b"
                        r"|\b(?:season|série|serie|sez[oó]na|řada|s)[ ._-]*(\d{1,2})(?![\d])", re.I)
# Díl pojmenovaný jen číslem: „01 Omluva Jesse Jacksonovi.mkv“, „E05 - Název.avi“, „12.mkv“
EP_ONLY = re.compile(r"^(?:e|ep|díl|dil)?[ ._-]*(?P<e>\d{1,3})(?P<title>[ ._-]+.*?)?(?P<ext>\.[^.]+)$", re.I)


def archive_season(text: str) -> int | None:
    """Číslo série z názvu archivu/složky, jen když je jediné: 'MĚSTEČKO SOUTH PARK 11. série (2007) CZ.rar' → 11,
    'Futurama.S08.1080p' → 8; rozsah ('1-12 komplet', 'S01-S07') nebo víc čísel → None (hádat se nemá)."""
    text = re.sub(r"(?:\.part\d+)?\.(?:rar|zip|7z)(?:\.\d{3})?$", "", text, flags=re.I)
    if _SEASON_RANGE.search(text):
        return None
    found = {int(a or b) for a, b in _SEASON_NO.findall(text)}
    return found.pop() if len(found) == 1 else None


def library_show(show: str, season: int) -> str:
    """Název seriálu pro díl, u kterého série přišla z názvu archivu: 'MĚSTEČKO SOUTH PARK 11' → 'South Park'
    (pryč číslo série na konci, pak sladit s knihovnou)."""
    return library_dir(re.sub(rf"[ ._-]+0?{season}\.?$", "", show).strip(" -") or show)


def library_dir(show: str) -> str:
    """Seriál už v knihovně je pod kratším názvem → jeho složka: 'Městečko South Park' → 'South Park'
    (jinak by Jellyfin ukázal dva seriály). Jen když se jím název končí a takový seriál je jediný."""
    lib = os.path.join(DOWNLOAD_ROOT, "serialy")
    if os.path.isdir(os.path.join(lib, show)):
        return show
    try:
        dirs = [d for d in os.listdir(lib) if os.path.isdir(os.path.join(lib, d))]
    except OSError:
        return show
    hits = [d for d in dirs if len(_fold(d)) >= 4 and _fold(show).endswith(_fold(d))]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        # Seriál pod jiným názvem (česky × anglicky: 'Červený trpaslík S13E01'
        # vedle 'Red Dwarf'): díl se stejným názvem seriálu už leží v jiné složce → tam
        want = _fold(show)
        known = {d for d in dirs if want and _has_episodes_of(os.path.join(lib, d), want)}
        if len(known) == 1:
            return known.pop()
    return show


def _has_episodes_of(show_dir: str, want: str) -> bool:
    """Je v serialy/<složka>/Season NN díl, jehož název seriálu (před SxxEyy) je po _fold() = want?"""
    try:
        seasons = [s for s in os.listdir(show_dir) if os.path.isdir(os.path.join(show_dir, s))]
        for s in seasons:
            for f in os.listdir(os.path.join(show_dir, s)):
                if (m := re.match(r"^(.+?)[ ._-]+S\d{1,2}E\d{1,3}", f, re.I)) and _fold(m[1]) == want:
                    return True
    except OSError:
        pass
    return False


def free_name(dest):
    """Na Linuxu rename/move existující soubor bez varování PŘEPÍŠE → když už v knihovně
    stejný název je, uložit jako „Název (2).mkv“, „Název (3).mkv“…"""
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


# ---------- svazky ----------
def volume_info(path: str) -> tuple[str, re.Pattern] | None:
    """Pro díl archivu vrátí (cesta k prvnímu svazku, vzor názvů všech svazků), jinak None.
    Film.part3.rar → Film.part1.rar | Film.r05 → Film.rar | Film.7z.002 → Film.7z.001 | Film.zip"""
    d, n = os.path.split(path)
    if m := re.match(r"^(.+)\.part(\d+)\.rar$", n, re.I):
        pat = re.compile(re.escape(m[1]) + r"\.part\d+\.rar$", re.I)
        first = f"{m[1]}.part{1:0{len(m[2])}d}.rar"
    elif m := re.match(r"^(.+)\.(?:rar|r\d{2,3})$", n, re.I):
        pat = re.compile(re.escape(m[1]) + r"\.(?:rar|r\d{2,3})$", re.I)
        first = m[1] + ".rar"
    elif m := re.match(r"^(.+\.(?:7z|zip|rar))\.\d{3}$", n, re.I):
        pat = re.compile(re.escape(m[1]) + r"\.\d{3}$", re.I)
        first = m[1] + ".001"
    elif re.search(r"\.(?:zip|7z)$", n, re.I):
        pat = re.compile(re.escape(n) + "$")
        first = n
    else:
        return None
    return os.path.join(d, first), pat


def playing_now() -> bool:
    try:
        return time.time() - os.path.getmtime(PLAYING_FILE) < PLAYING_FRESH
    except OSError:
        return False


def is_archive(name: str) -> bool:
    return volume_info(name) is not None


def _parts(first: str, pat: re.Pattern) -> list[str]:
    d = os.path.dirname(first)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if pat.fullmatch(f)) if os.path.isdir(d) else []


# ---------- stav ----------
@contextlib.contextmanager
def _state_lock():
    os.makedirs(LOCK_DIR, exist_ok=True)
    with open(os.path.join(LOCK_DIR, "state.lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def get_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write(st: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def set_state(gid: str, **fields) -> None:
    with _state_lock():
        st = get_state()
        st[gid] = {**st.get(gid, {}), **fields}
        _write(st)


def _set_archive(first: str, **fields) -> None:
    """Stejný stav všem dílům jednoho archivu (rozbalí se jednou, ukázat u všech částí)."""
    fields.setdefault("retry", False)
    with _state_lock():
        st = get_state()
        for v in st.values():
            if v.get("archive") == first:
                v.update(fields)
        _write(st)


def prune(known: set[str]) -> None:
    """aria2 stahování zapomněla (restart, Skrýt) → pryč i stav."""
    with _state_lock():
        st = get_state()
        gone = [g for g in st if g not in known and st[g].get("state") != "running"]
        if gone:
            for g in gone:
                del st[g]
            _write(st)


# ---------- 7z ----------
class Wait(Exception):
    """Ještě nejsou všechny části."""


class NeedPassword(Exception):
    pass


class Failed(Exception):
    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry               # má smysl „Zkusit znovu“ (místo na disku, chyba 7z), ne u zlého archivu


def _7z(args: list[str], archive: str, password: str) -> str:
    # -p vždy (i prázdné) → 7z se nikdy neptá na heslo z klávesnice. Přepínače PŘED „--“,
    # za ním by je 7z bral jako názvy souborů.
    r = subprocess.run(["7z", *args, f"-p{password}", "-y", "-bd", "--", archive], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, errors="replace")
    out = r.stdout + r.stderr
    if os.environ.get("EXTRACT_DEBUG"):
        log(f"7z {args[0]} {archive}: rc={r.returncode} {out[-400:]!r}")
    # Návratový kód sám nestačí: s ignorovaným SIGCHLD (dědí se z aria2) vrací Python vždy 0
    if r.returncode == 0 and not re.search(r"^(?:ERROR|Errors?):", out, re.M):
        return out
    low = out.lower()
    if "wrong password" in low or "encrypted" in low and ("can not open" in low or "data error" in low):
        raise NeedPassword("Špatné heslo" if password else "Archiv má heslo")
    if "missing volume" in low or "unexpected end of archive" in low:
        raise Wait()
    if "cannot open the file as archive" in low or "can not open the file as archive" in low:
        raise Failed("Soubor není archiv nebo je poškozený")
    if "there is not enough space" in low or "no space left" in low:
        raise Failed("Při rozbalování došlo místo na disku", retry=True)
    tail = [l for l in out.splitlines() if l.strip()][-2:]
    raise Failed("Rozbalení selhalo: " + " / ".join(tail)[:200], retry=True)


def list_entries(first: str, password: str) -> list[dict]:
    out = _7z(["l", "-slt"], first, password)
    entries, cur = [], None
    for line in out.splitlines():
        if line.startswith("Path = "):
            cur = {"path": line[7:]}
            entries.append(cur)
        elif cur is not None and " = " in line:
            k, v = line.split(" = ", 1)
            cur[k] = v
    return entries[1:]                       # první „Path =“ je archiv sám


_CZ = re.compile(r"[ěščřžýůňťďĚŠČŘŽÝŮŇŤĎ]")


def _cp852(name: str) -> str | None:
    """Název z rozbaleného zipu → správná čeština, nebo None (nesahat). Dva případy:
    - zip bez příznaku UTF-8 (DOS kódování CP852): 7z na Linuxu zapíše surové bajty → v Pythonu
      surrogate znaky; -mcp=852 nepomůže
    - názvy už při balení převedené CP852 → CP437 → UTF-8 („postiºen∞ch“, South Park z Webshare 24. 10.)"""
    raw = os.fsencode(name)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp852")
    try:
        return name.encode("cp437").decode("cp852")
    except UnicodeError:
        return None                     # opravdové UTF-8 (Č, ž…) do CP437 nejde


def zip_is_cp852(first: str, entries: list[dict]) -> bool:
    """Má zip názvy v CP852? Surové bajty se ve výpisu 7z ukážou jako „�“; rozbité UTF-8 pozná
    převod CP437 → CP852, který přidá české znaky."""
    if not re.search(r"\.zip(?:\.\d{3})?$", first, re.I):
        return False
    gained = 0
    for e in entries:
        if "�" in e["path"]:
            return True
        if (fixed := _cp852(e["path"])) is not None:
            gained += len(_CZ.findall(fixed)) - len(_CZ.findall(e["path"]))
    return gained > 0


def _recode_tree(tmp: str) -> None:
    """Přejmenuje rozbalené soubory i složky CP437 → CP852 (odspodu, ať se nemění cesta pod rukama)."""
    for root, dirs, files in os.walk(tmp, topdown=False):
        for n in files + dirs:
            fixed = _cp852(n)
            if fixed and fixed != n and "/" not in fixed:
                os.rename(os.path.join(root, n), os.path.join(root, fixed))


def _check_entries(entries: list[dict]) -> int:
    """Cesty ven z cílové složky a odkazy odmítnout (přes symlink by šlo zapsat mimo).
    Vrátí součet velikostí souborů."""
    total = 0
    for e in entries:
        p = e["path"].replace("\\", "/")
        if p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p):
            raise Failed(f"Archiv obsahuje nebezpečnou cestu: {p[:80]}")
        attrs = e.get("Attributes", "")
        if e.get("Symbolic Link") or e.get("Hard Link") or re.search(r"(?:^|\s|-)l[rwx-]{9}", attrs):
            raise Failed(f"Archiv obsahuje odkaz (symlink): {p[:80]}")
        if e.get("Folder") != "+":
            total += int(e.get("Size") or 0)
    return total


# ---------- hlavní ----------
def handle(gid: str, path: str, password: str = "") -> str:
    """Zpracuje díl archivu. Vrací výsledný stav (waiting/running/done/password/error)."""
    info = volume_info(path)
    if not info:
        return "none"
    first, pat = info
    rel = first[len(INCOMPLETE) + 1:] if first.startswith(INCOMPLETE + "/") else ""
    target = rel.split("/")[0]
    if target not in ("filmy", "serialy"):
        set_state(gid, state="error", msg="Archiv mimo složku stahování", path=path, archive=first)
        return "error"
    set_state(gid, path=path, archive=first, target=target)

    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, hashlib.md5(first.encode()).hexdigest() + ".lock")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "running"                 # rozbaluje už jiný díl / jiné volání
        try:
            parts = _parts(first, pat)
            if not os.path.exists(first) or any(os.path.exists(p + ".aria2") for p in parts):
                raise Wait()
            if playing_now():
                _set_archive(first, state="waiting", msg="Počká, až se v Jellyfinu dokouká (rozbalování by sekalo film)", hold=True)
                while playing_now():
                    time.sleep(HOLD_STEP)
            _set_archive(first, state="running", msg="Rozbaluji…", hold=False)
            n, skipped = _extract(first, target, password)
            for p in _parts(first, pat):
                os.remove(p)
            _prune_dirs(os.path.dirname(first))
            msg = f"Rozbaleno: {n} {'video' if n == 1 else 'videa' if 2 <= n <= 4 else 'videí'}"
            if skipped:
                msg += f" ({skipped} bez SxxEyy v názvu — Jellyfin je nemusí poznat)"
            _set_archive(first, state="done", msg=msg)
            jf_refresh(target)
            log(f"{first}: {msg}")
            return "done"
        except Wait:
            _set_archive(first, state="waiting", msg="Čeká na další části archivu")
            return "waiting"
        except NeedPassword as e:
            _set_archive(first, state="password", msg=str(e))
            return "password"
        except Failed as e:
            _set_archive(first, state="error", msg=str(e), retry=e.retry)
            log(f"{first}: {e}")
            return "error"
        except Exception as e:  # noqa: BLE001 — nesmí zůstat viset ve stavu „Rozbaluji…“
            _set_archive(first, state="error", msg=f"Neočekávaná chyba: {e.__class__.__name__}: {e}"[:200], retry=True)
            log(f"{first}: {e!r}")
            return "error"


def _extract(first: str, target: str, password: str) -> tuple[int, int]:
    entries = list_entries(first, password)
    if any(e.get("Encrypted") == "+" for e in entries) and not password:
        raise NeedPassword("Archiv má heslo")
    total = _check_entries(entries)
    free = shutil.disk_usage(DOWNLOAD_ROOT).free - SPACE_RESERVE
    if total > free:
        gib = lambda b: f"{b / 1024**3:.1f} GB"
        raise Failed(f"Nevejde se na disk: rozbalený má {gib(total)}, volné je {gib(max(free, 0))}", retry=True)

    tmp = os.path.join(INCOMPLETE, ".extract", hashlib.md5(first.encode()).hexdigest())
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        _7z(["x", f"-o{tmp}"], first, password)
        if zip_is_cp852(first, entries):
            _recode_tree(tmp)
        base = os.path.basename(first)
        return _sort_out(tmp, target, show_name(base), archive_season(base))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _guess_show(tmp: str, root: str, archive_show: str) -> str:
    """Díl jen „S05E01-Název.avi“ (bez seriálu): název z nejbližší složky v archivu, která není
    jen série („05“, „Season 5“), jinak z názvu archivu."""
    rel = os.path.relpath(root, tmp)
    for part in reversed([] if rel == "." else rel.split(os.sep)):
        if not SEASON_DIR.match(part.strip()) and (name := show_name(part)):
            return name
    return archive_show


def _guess_season(tmp: str, root: str, season: int | None) -> int | None:
    """Číslo série pro díl jen s číslem: nejbližší složka „11“ / „Season 11“ / „11. série“, jinak z názvu archivu."""
    rel = os.path.relpath(root, tmp)
    for part in reversed([] if rel == "." else rel.split(os.sep)):
        if SEASON_DIR.match(part.strip()):
            return int(re.search(r"\d{1,2}", part)[0])
        if (n := archive_season(part)) is not None:
            return n
    return season


def _fold(show: str) -> str:
    """'Cerveny-trpaslik' i 'Červený trpaslík' → 'cervenytrpaslik' (porovnání názvů seriálů)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", show).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _sort_out(tmp: str, target: str, archive_show: str = "", archive_season_no: int | None = None) -> tuple[int, int]:
    """Videa a titulky z rozbaleného archivu do knihovny. Ostatní (nfo, txt, exe…) se smaže s tmp."""
    real_tmp = os.path.realpath(tmp)
    plan = []                                   # (soubor, nový název, podsložka, je video)
    for root, _dirs, files in os.walk(tmp):
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full) or not os.path.realpath(full).startswith(real_tmp + "/"):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXT:
                if "sample" in f.lower() and os.path.getsize(full) < SAMPLE_MAX:
                    continue
            elif ext not in SUB_EXT:
                continue
            name, sub = f, ""
            if target == "serialy":
                name = normalize_episode(f)
                sub = series_subdir(name)
                if not sub and re.match(r"S\d{1,2}E\d{1,3}", name, re.I):
                    if show := _guess_show(tmp, root, archive_show):
                        name = f"{show} {name}"
                        sub = series_subdir(name)
                # díl jen s číslem („01 Omluva Jesse Jacksonovi.mkv“, 1. 11. 2026 South Park 11. série):
                # série ze složky/názvu archivu, seriál ze složky/archivu sladěný s knihovnou
                if not sub and (ep := EP_ONLY.match(name)) \
                        and (season := _guess_season(tmp, root, archive_season_no)) is not None \
                        and (show := _guess_show(tmp, root, archive_show)):
                    show = library_show(show, season)
                    title = (ep["title"] or "").strip(" ._-")
                    name = f"{show} S{season:02d}E{int(ep['e']):02d}{' - ' + title if title else ''}{ep['ext']}"
                    sub = series_subdir(name)
            plan.append((full, library_name(name, sub), sub, ext in VIDEO_EXT))

    # Archiv = jeden seriál, ale díly bývají pojmenované různě („Red Dwarf S05E01“, „Cerveny-trpaslik
    # S12E01“) → v Jellyfinu by vznikly 3 seriály. Když se názvy v archivu liší, složku určí název
    # archivu (sérii číslo v názvu dílu). Shodné názvy dílů mají přednost (Show.nos.rar → „Show“).
    shows = {_fold(sub.split("/")[1]) for _, _, sub, _ in plan if sub}
    if len(shows) > 1 and archive_show:
        plan = [(full, name, f"/{drop_season_suffix(archive_show, int(m.group(1)))}/Season {int(m.group(1)):02d}"
                 if sub and (m := re.search(r"S(\d{1,2})E\d{1,3}", name, re.I)) else sub, video)
                for full, name, sub, video in plan]

    moved = skipped = 0
    for full, name, sub, video in plan:
        dest_dir = os.path.join(DOWNLOAD_ROOT, target) + sub
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(full, free_name(os.path.join(dest_dir, name)))
        moved += video
        skipped += video and target == "serialy" and not sub
    if not moved:
        raise Failed("V archivu nebylo žádné video")
    return moved, skipped


def _prune_dirs(d: str) -> None:
    while d.startswith(INCOMPLETE + "/") and d != INCOMPLETE:
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


# ---------- Jellyfin ----------
def jf_refresh(rel: str) -> None:
    """Sken knihovny, do které soubor přibyl. rel = 'filmy/…' nebo 'serialy/…' (stačí i jen cíl)."""
    if not (JF_URL and JF_TOKEN):
        log("JELLYFIN_URL/TOKEN nenastaveno, sken přeskočen")
        return
    # Jellyfin vidí /downloads jako JF_MEDIA_PATH (výchozí /media, viz docker-compose / .env).
    # Rychlá cesta: refresh celé knihovny (Filmy/Seriály), jejíž kořen odpovídá cíli
    # → položka je v knihovně do ~1 s. (Library/Media/Updated Jellyfin schválně
    # 60 s hromadí, proto ho používáme jen jako fallback.)
    jf_root = f"{JF_MEDIA_PATH}/" + rel.split("/")[0]   # /media/filmy | /media/serialy
    headers = {"Content-Type": "application/json",
               "Authorization": f'MediaBrowser Token="{JF_TOKEN}"'}
    try:
        req = urllib.request.Request(f"{JF_URL}/Library/VirtualFolders", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            libs = json.load(r)
        lib = next((l for l in libs if jf_root in (l.get("Locations") or [])), None)
        if lib:
            url = f"{JF_URL}/Items/{lib['ItemId']}/Refresh?Recursive=true"
            req = urllib.request.Request(url, data=b"", method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                log(f"Jellyfin refresh knihovny '{lib['Name']}': HTTP {r.status}")
            return
        log(f"knihovna pro {jf_root} nenalezena, fallback Media/Updated")
    except Exception as e:  # noqa: BLE001
        log(f"refresh knihovny selhal ({e}), fallback Media/Updated")

    jf_path = f"{JF_MEDIA_PATH}/" + (os.path.dirname(rel) or rel)
    body = json.dumps({"Updates": [{"Path": jf_path, "UpdateType": "Created"}]}).encode()
    req = urllib.request.Request(f"{JF_URL}/Library/Media/Updated", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"Jellyfin Media/Updated {jf_path}: HTTP {r.status}")
    except Exception as e:  # noqa: BLE001
        log(f"Jellyfin sken selhal: {e}")
