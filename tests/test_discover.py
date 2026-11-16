"""Testy výběru verzí z Webshare (discover.py) — bez sítě. Spuštění (z kořene repozitáře, image stahovac z docker compose build):
  docker run --rm --entrypoint python -v "$PWD/tests:/t:ro" stahovac /t/test_discover.py
"""
import os, sys
sys.path.insert(0, "/app")
os.chdir("/app")
import discover as D

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

GB = 1024**3
def f(name, gb=5.0, **kw):
    return {"ident": name[:10], "name": name, "size": int(gb * GB), **kw}
def cl(name, kind="movie", titles=("Duna", "Dune"), year=2021, gb=5.0):
    return D.classify(f(name, gb), kind, list(titles), year)

# kvalita a jazyk z názvu
w = lambda n: D.fold(n).split()
check("2160p = 4K", D.quality(w("Film.2160p.HEVC"))[1] == "4K")
check("1080p + DS4K = 1080p", D.quality(w("Film.IMAX.1080p.10bit.DS4K"))[1] == "1080p")
check("UHDRDV = 4K", D.quality(w("Film.(2026).CZ.4K.UHDRDV"))[1] == "4K")
check("bez značky = neznámá", D.quality(w("Film (2026)"))[0] == 0)
check("CZ dabing", D.language(w("Film CZ dabing 1080p"))[1] == "CZ dabing")
check("samotné CZ = dabing", D.language(w("Film (SK,CZ,EN)[2160p]"))[1] == "CZ dabing")
check("CZ titulky", D.language(w("Film 2160p EN CZ titulky"))[1] == "CZ titulky")
check("titulky.CZ = titulky", D.language(w("Film.EN.titulky.CZ.SK"))[1] == "CZ titulky")
check("CZ zvuk + CZ titulky = dabing", D.language(w("Film 4K CZ 2.0 EN Atmos CZ SK titulky"))[1] == "CZ dabing")
check("CZ.subt = titulky", D.language(w("Film.audio.EN,CZ.subt.EN"))[1] == "CZ titulky")
check("slepené (2026)CZtit = titulky", D.language(w("Hopeu - Hope (2026)CZtit 1080p"))[1] == "CZ titulky")
check("slepené CZtitulky = titulky", D.language(w("Film.2026.CZtitulky.1080p"))[1] == "CZ titulky")
check("slepené CZsub = titulky", D.language(w("Film 2026 CZsub"))[1] == "CZ titulky")
check("slepené CZdabing = dabing", D.language(w("Film 2026 CZdabing"))[1] == "CZ dabing")
check("CZSK (zvuk) zůstává dabing", D.language(w("Film 2026 CZSK 1080p"))[1] == "CZ dabing")
check("ESub (angl.) ≠ čeština", D.language(w("Film.Hindi.English.ESub"))[0] == 0)

# co k titulu nepatří
check("jiný film se stejným slovem", cl("Dune Drifter 2020 SK titulky.mp4") is None)
check("krátký název bez roku pryč", cl("dune_part_one.mkv", gb=20) is None)
check("krátký název s rokem ok", cl("Dune (2021) CZ EN 1080p.mkv") is not None)
check("díl seriálu u filmu pryč", cl("Dune.Prophecy.S01E01.2160p.mkv") is None)
check("trailer pryč", cl("Dune (2021) Trailer 4K.mkv") is None)
check("malý soubor pryč", cl("Dune (2021) CZ.mkv", gb=0.2) is None)
check("ne-video pryč", cl("Dune (2021) CZ.rar") is None)
sm = ("Spider-Man: Zbrusu nový den", "Spider-Man: Brand New Day")
check("dlouhý název bez roku ok", cl("Spider-Man Zbrusu nový den CZ SK dab.mkv", titles=sm, year=2026) is not None)
check("dlouhý název, jiný rok pryč", cl("Brand.New.Day.2009.1080p.mkv", titles=sm, year=2026) is None)
check("číslo v názvu není rok", cl("Blade Runner 2049 (2017) CZ 2160p.mkv", titles=("Blade Runner 2049",), year=2017) is not None)
check("seriál: díl zůstane", (cl("Dune.Prophecy.S01E02.1080p.CZ.mkv", kind="tv", titles=("Duna: Proroctví", "Dune: Prophecy"), year=2024, gb=1) or {}).get("ep") == [1, 2])

# pořadí: čeština > 4K > 1080p > dabing > titulky; obří remux až za rozumným 4K
files = [cl(n, titles=sm, year=2026, gb=g) for n, g in [
    ("Spider-Man Brand New Day 2026 2160p HDR.mkv", 20),          # 4K bez češtiny
    ("Spider-Man Zbrusu nový den 2026 CZ dabing 1080p.mkv", 5),
    ("Spider-Man Zbrusu nový den 2026 2160p CZ titulky.mkv", 25),
    ("Spider-Man Zbrusu nový den 2026 4K CZ dabing.mkv", 87),     # remux
    ("Spider-Man Zbrusu nový den 2026 2160p CZ dabing.mkv", 27),
]]
order = [x["name"] for x in D.rank(files)]
check("nejlepší = 4K CZ dabing rozumná velikost", order[0].endswith("2160p CZ dabing.mkv"), order)
check("pak 4K remux s dabingem", "4K CZ dabing" in order[1], order)
check("pak 4K titulky", "CZ titulky" in order[2], order)
check("1080p dabing před 4K bez češtiny", order.index("Spider-Man Zbrusu nový den 2026 CZ dabing 1080p.mkv") < order.index("Spider-Man Brand New Day 2026 2160p HDR.mkv"), order)
s = D.summary(D.rank(files))
check("souhrn", s["best_q"] == "4K" and s["best_lang"] == "CZ dabing" and s["has_4k_cz"] and s["count"] == 5, s)

# štítek „nově ve 4K“
import time
now = time.time()
check("4K až po přidání = nové", D.new_4k({"added": now - 86400}, {"first_4k": now - 3600}))
check("4K už při přidání = ne", not D.new_4k({"added": now - 3600}, {"first_4k": now - 3600}))
check("viděno = ne", not D.new_4k({"added": now - 86400, "seen_4k": True}, {"first_4k": now - 3600}))
check("starší 14 dní = ne", not D.new_4k({"added": now - 40 * 86400}, {"first_4k": now - 20 * 86400}))

print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
