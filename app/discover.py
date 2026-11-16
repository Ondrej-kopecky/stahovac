"""
Objevit + wishlist: co je nového / dobře hodnoceného (TMDB) → wishlist (každý uživatel svůj)
→ hledání verzí na Webshare → nejlepší verze jedním klikem do stahování.

TMDB ví, co vyšlo a jak je to hodnocené, ale ne jestli je to na Webshare ve 4K — to je vlastnost
konkrétního souboru. Proto: názvy souborů z Webshare hledání → kvalita (4K/1080p/720p) a jazyk
(CZ dabing / CZ titulky) z názvu → seřadit podle preferencí:
  něco české > nic českého,  pak 4K > 1080p > 720p > neznámá,  pak dabing > titulky.
4K bez češtiny je pro rodinu horší než 1080p s dabingem — proto čeština první.

Hlídač na pozadí (app.py) projde wishlisty každých WATCH_EVERY a zapíše, co je k dispozici
(/data/wish_avail.json) → štítek „nově ve 4K“, když se 4K s češtinou objeví až po přidání.

Funkce tady jsou čisté (bez FastAPI), ať jdou testovat bez sítě — HTTP je v app.py.
"""
import json
import os
import re
import time
import unicodedata

import httpx

TMDB_API = "https://api.themoviedb.org/3"
TMDB_TOKEN = os.environ.get("TMDB_TOKEN", "")
TMDB_IMG = "https://image.tmdb.org/t/p/"
LANG, REGION = "cs-CZ", "CZ"
KINDS = {"movie", "tv"}

WISH_FILE = "/data/wishlist.json"
AVAIL_FILE = "/data/wish_avail.json"
WATCH_EVERY = 6 * 3600              # hlídač: jednou za 6 h (Webshare hledání nechceme zahltit)
NEW_4K_DAYS = 14                    # štítek „nově ve 4K“ svítí max. 14 dní (nebo do otevření)

VIDEO_EXT = {"mkv", "mp4", "avi", "m4v", "mov", "ts", "wmv", "webm"}
MIN_SIZE = {"movie": 500 * 1024**2, "tv": 80 * 1024**2}     # menší = trailer, ukázka, mobilní verze
COMFY_4K = 40 * 1024**3             # 4K nad 40 GB (remux 80+ GB) jen když není nic menšího


# ---------- TMDB ----------
_cache: dict[str, tuple[float, object]] = {}


async def tmdb(client: httpx.AsyncClient, path: str, ttl: int = 3600, **params):
    """GET na TMDB s cache v paměti (seznamy se mění pomalu, limit TMDB ~50 req/s je daleko)."""
    if not TMDB_TOKEN:
        raise RuntimeError("Chybí TMDB_TOKEN v .env")
    params = {"language": LANG, **{k: v for k, v in params.items() if v not in (None, "")}}
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    r = await client.get(f"{TMDB_API}{path}", params=params, timeout=15,
                         headers={"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"TMDB {r.status_code}: {r.json().get('status_message', '') if r.content else ''}")
    data = r.json()
    if len(_cache) > 500:                               # ať cache neroste donekonečna
        _cache.clear()
    _cache[key] = (time.time(), data)
    return data


def _day(offset_days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + offset_days * 86400))


# Seznamy na stránce Objevit. „Nové“ u filmů = vyšlo digitálně / na Blu-ray (typ vydání 4|5) za
# posledních 120 dní — to je chvíle, kdy se film objevuje na Webshare (kino verze ještě ne).
def discover_query(kind: str, lst: str, genre: str = "", min_rating: float = 0, page: int = 1) -> tuple[str, dict]:
    p: dict = {"page": page, "with_genres": genre or None}
    if min_rating:
        p["vote_average.gte"] = min_rating
    if lst == "trending" and not genre and not min_rating:
        return f"/trending/{kind}/week", {"page": page}
    if lst == "top":
        p.update({"sort_by": "vote_average.desc", "vote_count.gte": 1500 if kind == "movie" else 400})
    elif lst == "new" and kind == "movie":
        p.update({"sort_by": "popularity.desc", "region": REGION, "with_release_type": "4|5",
                  "release_date.gte": _day(-120), "release_date.lte": _day(0), "vote_count.gte": 20})
    elif lst == "new":                                  # seriály: běží teď (díl za poslední 2 měsíce)
        p.update({"sort_by": "popularity.desc", "air_date.gte": _day(-60), "air_date.lte": _day(0),
                  "vote_count.gte": 30, "first_air_date.gte": _day(-3 * 365)})
    else:                                               # populární, nebo trendy s filtrem
        p.update({"sort_by": "popularity.desc", "vote_count.gte": 100})
    return f"/discover/{kind}", p


def card(kind: str, x: dict) -> dict:
    """TMDB výsledek → to, co potřebuje karta na webu."""
    date = x.get("release_date") if kind == "movie" else x.get("first_air_date")
    return {
        "kind": kind, "id": x["id"],
        "title": x.get("title") or x.get("name") or "",
        "orig": x.get("original_title") or x.get("original_name") or "",
        "year": int(date[:4]) if date and date[:4].isdigit() else None,
        "rating": round(x.get("vote_average") or 0, 1), "votes": x.get("vote_count") or 0,
        "poster": x.get("poster_path"), "backdrop": x.get("backdrop_path"),
        "overview": x.get("overview") or "",
        "genre_ids": x.get("genre_ids") or [g["id"] for g in x.get("genres") or []],
    }


# ---------- Webshare soubory: kvalita, jazyk, pořadí ----------
def fold(s: str) -> str:
    """Bez diakritiky, malá písmena, jen písmena a čísla oddělená mezerou."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


STOP = {"the", "a", "an", "of", "and", "i", "v", "na", "se", "z", "s", "to", "je"}


def title_words(title: str) -> set[str]:
    return {w for w in fold(title).split() if w not in STOP} or set(fold(title).split())


def quality(words: list[str]) -> tuple[int, str]:
    """(pořadí, popisek). Explicitní 1080p má přednost před „DS4K“ apod. (zdroj 4K, výstup 1080p)."""
    joined = " ".join(words)
    if re.search(r"\b2160p?\b", joined):
        return 3, "4K"
    if re.search(r"\b1080[pi]?\b|\bfhd\b|\bfullhd\b", joined):
        return 2, "1080p"
    if re.search(r"\b4k\b|\buhd\w*", joined):
        return 3, "4K"
    if re.search(r"\b720p?\b|\bhd\b", joined):
        return 1, "720p"
    if re.search(r"\b(480p?|576p?|dvdrip|tvrip|xvid|cam|ts|hdcam)\b", joined):
        return -1, "SD"
    return 0, ""


SUBS = {"titulky", "tit", "sub", "subs", "subt", "esub", "titles", "titl", "subtitles", "titulkami"}
DUB = {"dabing", "dab", "czdab", "dabovano", "dabovany", "czdabing"}


def language(words: list[str]) -> tuple[int, str]:
    """(pořadí, popisek): 2 = CZ dabing, 1 = CZ titulky, 0 = nic českého.
    Samotné „CZ“ v názvu znamená skoro vždy zvuk — kromě „CZ titulky“ / „titulky CZ“."""
    # Slepené „(2026)CZtit“, „CZtitulky“, „CZsub“ → „cz tit“, jinak by je nic nepoznalo
    split = []
    for w in words:
        m = re.fullmatch(r"(czech|cze|cz)(\w+)", w)
        split += [m.group(1), m.group(2)] if m and m.group(2) in SUBS | DUB else [w]
    words = split
    if DUB & set(words):
        return 2, "CZ dabing"
    cz_audio = cz_sub = False
    for k, w in enumerate(words):
        if w in ("cz", "cze", "czech", "cesky", "cs", "czsk"):
            near = set(words[k + 1:k + 3]) | set(words[max(0, k - 2):k])
            if near & SUBS:
                cz_sub = True
            else:
                cz_audio = True
    if cz_audio:
        return 2, "CZ dabing"
    if cz_sub or {"titulky", "tit"} & set(words):       # „titulky“ samo = české (Webshare je CZ/SK)
        return 1, "CZ titulky"
    return 0, ""


def classify(f: dict, kind: str, titles: list[str], year: int | None) -> dict | None:
    """Soubor z Webshare hledání → popis s kvalitou, jazykem a skóre; None = nepatří k titulu."""
    name = f["name"]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in VIDEO_EXT or f["size"] < MIN_SIZE[kind] or f.get("password"):
        return None
    words = fold(name.rsplit(".", 1)[0]).split()      # bez přípony (.ts by jinak vypadalo jako „TS“ kvalita)
    wset = set(words)
    if wset & {"trailer", "teaser", "sample", "ukazka"}:
        return None
    # Všechna slova aspoň jednoho názvu (český / originál) musí být v názvu souboru
    matched = [t for t in (title_words(x) for x in titles) if t and t <= wset]
    if not matched:
        return None
    # Rok: když soubor nějaký má a žádný nesedí (±1, různá data vydání), je to jiný film/remake.
    # Krátký název (1–2 slova: „Duna“, „Zvířata“) je v názvech souborů všude („Dune Drifter 2020“)
    # → u něj rok v názvu povinný a přesný.
    if year and kind == "movie":
        years = {int(w) for w in words if re.fullmatch(r"(19|20)\d\d", w)}
        years -= {int(w) for t in titles for w in fold(t).split() if w.isdigit()}   # „Blade Runner 2049“
        short = max(len(t) for t in matched) <= 2
        if short and year not in years:
            return None
        if years and not any(abs(y - year) <= 1 for y in years):
            return None
    ep = re.search(r"\bs(\d{1,2}) ?e(\d{1,3})\b|\b(\d{1,2})x(\d{2,3})\b", " ".join(words))
    if ep and kind == "movie":                          # „Duna“ ≠ díly seriálu „Dune Prophecy“
        return None
    q_rank, q = quality(words)
    l_rank, lang = language(words)
    return {
        "ident": f["ident"], "name": name, "size": f["size"],
        "votes": f.get("positive_votes", 0) - f.get("negative_votes", 0),
        "q": q, "q_rank": q_rank, "lang": lang, "l_rank": l_rank,
        "ep": [int(ep.group(1) or ep.group(3)), int(ep.group(2) or ep.group(4))] if ep else None,
    }


ARCHIVE_EXT = {"rar", "zip", "7z"}
PACK_MIN = 300 * 1024**2            # celá série v archivu; menší = titulky, obrázky, hra
# Archivy, které nejsou díly seriálu (South Park: hry „Stick of Truth“, trenažéry, soundtrack)
PACK_JUNK = {"game", "hra", "hry", "pc", "ps3", "ps4", "ps5", "xbox", "crack", "cracked", "trainer", "dlc", "repack",
             "iso", "nodvd", "soundtrack", "ost", "mp3", "flac", "komiks", "kniha", "ebook", "pdf", "epub"}


def classify_pack(f: dict, titles: list[str]) -> dict | None:
    """Archiv s celou sérií z Webshare („South Park 11. série CZ DABING.zip“) → řádek „celá série“;
    pack = číslo série, 0 = víc sérií / komplet. Vícedílné archivy (.part2.rar, .r01, .001) zatím ne:
    bez všech částí by rozbalení čekalo navždy."""
    from extract import _SEASON_RANGE, archive_season    # extract discover neimportuje → bez cyklu
    name = f["name"]
    low = name.lower()
    if f["size"] < PACK_MIN or f.get("password") or "." not in name or low.rsplit(".", 1)[-1] not in ARCHIVE_EXT:
        return None
    if re.search(r"\.part\d+\.rar$", low):
        return None
    stem = name.rsplit(".", 1)[0]
    words = fold(stem).split()
    wset = set(words)
    if wset & PACK_JUNK or not [t for t in (title_words(x) for x in titles) if t and t <= wset]:
        return None
    season = archive_season(name)
    if season is None:
        if not (_SEASON_RANGE.search(stem) or wset & {"komplet", "kompletni", "complete"}):
            return None
        season = 0
    q_rank, q = quality(words)
    l_rank, lang = language(words)
    return {
        "ident": f["ident"], "name": name, "size": f["size"],
        "votes": f.get("positive_votes", 0) - f.get("negative_votes", 0),
        "q": q, "q_rank": q_rank, "lang": lang, "l_rank": l_rank, "ep": None, "pack": season,
    }


def rank_key(x: dict):
    """Řazení od nejlepšího: čeština > kvalita > dabing nad titulky > ne obří remux > hlasy > velikost."""
    return (x["l_rank"] > 0, x["q_rank"], x["l_rank"], not (x["q_rank"] == 3 and x["size"] > COMFY_4K),
            x["votes"], x["size"])


def rank(files: list[dict]) -> list[dict]:
    return sorted(files, key=rank_key, reverse=True)


def summary(ranked: list[dict]) -> dict:
    """Co je k dispozici (pro štítek ve wishlistu)."""
    best = ranked[0] if ranked else None
    return {
        "count": len(ranked),
        "best_q": best["q"] if best else "", "best_lang": best["lang"] if best else "",
        "has_4k_cz": any(x["q_rank"] == 3 and x["l_rank"] > 0 for x in ranked),
    }


# ---------- úložiště: wishlist (po uživatelích) + dostupnost (sdílená) ----------
def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


WISH: dict[str, list[dict]] = _load(WISH_FILE)          # uživatel → [položka]
AVAIL: dict[str, dict] = _load(AVAIL_FILE)              # "movie:123" → souhrn + first_4k


def key(kind: str, tmdb_id: int) -> str:
    return f"{kind}:{tmdb_id}"


def save_wish() -> None:
    _save(WISH_FILE, WISH)


def record_avail(k: str, ranked: list[dict]) -> dict:
    """Zapíše dostupnost. first_4k = kdy jsme 4K s češtinou viděli poprvé (základ štítku „nově“)."""
    old = AVAIL.get(k, {})
    s = summary(ranked)
    s["checked"] = int(time.time())
    s["first_4k"] = old.get("first_4k") or (s["checked"] if s["has_4k_cz"] else None)
    AVAIL[k] = s
    _save(AVAIL_FILE, AVAIL)
    return s


def new_4k(item: dict, av: dict) -> bool:
    """4K s češtinou se objevilo až PO přidání do wishlistu, nedávno, a uživatel to ještě neotevřel."""
    f = av.get("first_4k")
    return bool(f and f > item.get("added", 0) + 60 and time.time() - f < NEW_4K_DAYS * 86400
                and not item.get("seen_4k"))
