"""Testy rozbalování archivů (extract.py) nad skutečnými archivy. Archivy vyrobí
tests/make_arc_fixtures.sh (RAR po částech, s heslem, zlé zipy…). Běží v throwaway kontejneru:
/downloads a /data jsou dočasné. Spuštění (z kořene repozitáře, image stahovac z docker compose build):
  FX=$(mktemp -d); tests/make_arc_fixtures.sh $FX
  docker run --rm --entrypoint python -u 1000:1000 -v "$PWD/tests:/t:ro" -v "$FX:/fx:ro" \
    -v "$(mktemp -d):/downloads" -v "$(mktemp -d):/data" stahovac /t/test_extract.py
"""
import os, shutil, sys
sys.path.insert(0, "/app")
os.chdir("/app")
import extract as X

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

INC = X.INCOMPLETE
def put(target, *names):
    d = os.path.join(INC, target); os.makedirs(d, exist_ok=True)
    for n in names:
        shutil.copy(os.path.join("/fx", n), os.path.join(d, n))
    return d
def files(rel):
    root = os.path.join("/downloads", rel)
    return sorted(os.path.relpath(os.path.join(r, f), root) for r, _, fs in os.walk(root) for f in fs)
st = lambda gid: X.get_state().get(gid, {})

# 0) rozpoznání svazků
for n, first in [("a/F.part2.rar", "a/F.part1.rar"), ("a/F.part03.rar", "a/F.part01.rar"), ("a/F.r05", "a/F.rar"),
                 ("a/F.rar", "a/F.rar"), ("a/F.7z.002", "a/F.7z.001"), ("a/F.zip", "a/F.zip"), ("a/F.zip.001", "a/F.zip.001")]:
    check(f"svazek {n} → {first}", X.volume_info(n)[0] == first, X.volume_info(n))
check("mkv není archiv", not X.is_archive("Film.mkv") and not X.is_archive("Film.2020.mkv"))

# 1) RAR5 po částech: chybí část 2 → čeká; část 2 se ještě stahuje → čeká; pak rozbalí jednou
d = put("serialy", "Futurama.S08.1080p.part1.rar", "Futurama.S08.1080p.part3.rar")
p1, p2, p3 = (os.path.join(d, f"Futurama.S08.1080p.part{i}.rar") for i in (1, 2, 3))
check("chybí část → čeká", X.handle("g1", p1) == "waiting" and st("g1")["state"] == "waiting", st("g1"))
check("část 3 hotová, 2 chybí → čeká", X.handle("g3", p3) == "waiting")
shutil.copy("/fx/Futurama.S08.1080p.part2.rar", p2); open(p2 + ".aria2", "w").close()
check("část 2 se ještě stahuje (.aria2) → čeká", X.handle("g3", p3) == "waiting")
os.remove(p2 + ".aria2")
check("poslední část → rozbaleno", X.handle("g2", p2) == "done", st("g2"))
got = files("serialy")
want = sorted([f"Futurama/Season 08/Futurama.S08E0{e}.Nazev.dilu.1080p{x}" for e in (1, 2, 3) for x in (".mkv", ".cz.srt")])
check("díly + titulky v Season 08, bez sample/nfo/exe", got == want, got)
check("všechny části dostaly stav done", all(st(g)["state"] == "done" for g in ("g1", "g2", "g3")), X.get_state())
check("hláška „Rozbaleno: 3 videa“", st("g1")["msg"] == "Rozbaleno: 3 videa", st("g1")["msg"])
check("archiv i tmp smazané", not os.path.exists(os.path.join(INC, "serialy")) and not os.listdir(os.path.join(INC, ".extract")),
      os.listdir(INC))
shutil.rmtree("/downloads/serialy")

# 2) starý formát .rar + .r00 + .r01
d = put("serialy", "Futurama.old.rar", "Futurama.old.r00", "Futurama.old.r01")
check("starý formát .r00 → rozbaleno", X.handle("o1", os.path.join(d, "Futurama.old.r01")) == "done", st("o1"))
check("starý formát: 3 díly", len([f for f in files("serialy") if f.endswith(".mkv")]) == 3, files("serialy"))
shutil.rmtree("/downloads/serialy")

# 3) heslo v hlavičkách (ani výpis bez hesla) / jen u dat
for arc in ("Heslo.hlav.rar", "Heslo.data.rar"):
    d = put("filmy", arc); p = os.path.join(d, arc)
    check(f"{arc}: bez hesla → chce heslo", X.handle("h", p) == "password" and st("h")["msg"] == "Archiv má heslo", st("h"))
    check(f"{arc}: špatné heslo", X.handle("h", p, "spatne") == "password" and st("h")["msg"] == "Špatné heslo", st("h"))
    check(f"{arc}: správné heslo → film", X.handle("h", p, "Tajne123") == "done" and files("filmy") == ["Film (2020).mkv"],
          (st("h"), files("filmy")))
    shutil.rmtree("/downloads/filmy")

# 4) díly bez „S“ (Fastshare styl) → S01E01 + složka
d = put("serialy", "Show.nos.rar")
check("díly bez S → rozbaleno", X.handle("n", os.path.join(d, "Show.nos.rar")) == "done", st("n"))
check("díly bez S → Show/Season 01/Show S01E0x Nazev.mkv",
      files("serialy") == ["Show/Season 01/Show S01E01 Nazev.mkv", "Show/Season 01/Show S01E02 Nazev.mkv"], files("serialy"))
shutil.rmtree("/downloads/serialy")

# 5) zip s filmem ve složce, 7z po částech
d = put("filmy", "Film (2020).zip")
check("zip → film bez readme", X.handle("z", os.path.join(d, "Film (2020).zip")) == "done" and files("filmy") == ["Film (2020).mkv"],
      (st("z"), files("filmy")))
d = put("filmy", "Film7z.7z.001", "Film7z.7z.002")
check("7z po částech → film, kolize → (2)", X.handle("s", os.path.join(d, "Film7z.7z.002")) == "done"
      and files("filmy") == ["Film (2020) (2).mkv", "Film (2020).mkv"], (st("s"), files("filmy")))
shutil.rmtree("/downloads/filmy")

# 6) zlé archivy: nic se nesmí zapsat mimo, archiv zůstane (smaže ho až Skrýt)
for arc, gid, msg in [("Zly.zip", "e1", "nebezpečnou cestu"), ("Symlink.zip", "e2", "odkaz"), ("Bezvidea.zip", "e3", "žádné video")]:
    d = put("filmy", arc)
    r = X.handle(gid, os.path.join(d, arc))
    check(f"{arc} → chyba ({msg})", r == "error" and msg in st(gid)["msg"], st(gid))
    check(f"{arc}: archiv zůstal v .incomplete", os.path.exists(os.path.join(d, arc)))
check("zlý archiv: bez „Zkusit znovu“", not any(st(g).get("retry") for g in ("e1", "e2", "e3")))
check("nic se nezapsalo mimo", not os.path.exists("/ven.mkv") and not os.path.exists("/downloads/ven.mkv")
      and not os.path.exists("/downloads/filmy"), os.listdir("/downloads"))
check("tmp po chybě uklizen", not os.listdir(os.path.join(INC, ".extract")))

d = put("filmy", "Film (2020).zip")
open(os.path.join(d, "Rozbity.rar"), "wb").write(os.urandom(5000))
check("náhodná data .rar → chyba", X.handle("b", os.path.join(d, "Rozbity.rar")) == "error", st("b"))
X.SPACE_RESERVE = 10**15
check("nevejde se → chyba", X.handle("m", os.path.join(d, "Film (2020).zip")) == "error" and "Nevejde" in st("m")["msg"], st("m"))
check("nevejde se → nabídnout Zkusit znovu", st("m").get("retry") is True, st("m"))
X.SPACE_RESERVE = 0

# 6b) díly bez názvu seriálu („05/S05E01-Hololod.avi“ — Červený trpaslík z Webshare, 13. 10.)
import zipfile
for t, want in [("Red Dwarf - Červený trpaslík 1-12 komplet.zip", "Red Dwarf"), ("Futurama.S01-S07.1080p.CZ.part1.rar", "Futurama"),
                ("The Office (US) Season 1-9.7z.001", "The Office (US)"), ("Červený trpaslík 1-12.zip", "Červený trpaslík"),
                ("South-park-S16-CZ-HD-720p-16.Série_Uploader.rar", "South park"),      # pomlčky + podpis uploadera
                ("VisionQuest.S01E06.Deadlock.2160p.mkv", "VisionQuest")]:
    check(f"show_name {t!r} → {want!r}", X.show_name(t) == want, X.show_name(t))
for dname in ("05", "Season 5", "S05", "5. série", "2. sezóna"):
    check(f"složka {dname!r} = jen série", bool(X.SEASON_DIR.match(dname)))
X.SPACE_RESERVE = 0
d = os.path.join(INC, "serialy"); os.makedirs(d, exist_ok=True)
with zipfile.ZipFile(os.path.join(d, "Red Dwarf - Červený trpaslík 1-12 komplet.zip"), "w") as z:
    z.writestr("05/S05E01-Hololod-Holoship.avi", "v" * 1000)
    z.writestr("10/S10E01-Trojan---HD.1080p.mkv", "v" * 1000)
    z.writestr("Red Dwarf S11/S11E01-Twentica.mkv", "v" * 1000)        # název ze složky
    z.writestr("12/Cerveny-trpaslik S12E01 Skippers.mkv", "v" * 1000)  # jinak pojmenovaný díl → stejný seriál
    z.writestr("Bonus/Making of.avi", "v" * 1000)                       # bez SxxEyy → kořen + hláška
check("díly bez názvu → rozbaleno", X.handle("rd", os.path.join(d, "Red Dwarf - Červený trpaslík 1-12 komplet.zip")) == "done", st("rd"))
got = files("serialy")
check("díly bez názvu → Red Dwarf/Season NN/Red Dwarf SxxEyy…",
      got == ["Making of.avi", "Red Dwarf/Season 05/Red Dwarf S05E01-Hololod-Holoship.avi",
              "Red Dwarf/Season 10/Red Dwarf S10E01-Trojan---HD.1080p.mkv", "Red Dwarf/Season 11/Red Dwarf S11E01-Twentica.mkv",
              "Red Dwarf/Season 12/Cerveny-trpaslik S12E01 Skippers.mkv"], got)
check("hláška zmíní nezařazené", "1 bez SxxEyy" in st("rd")["msg"], st("rd")["msg"])
shutil.rmtree("/downloads/serialy")

# 6c) South Park z Webshare (24. 10.): číslo série v názvu seriálu + zip s názvy v CP852 (bez příznaku UTF-8)
check("show_name South Park 06. série … BEZ HESLA", X.show_name("South Park 06. série CZ DABING BEZ HESLA.zip") == "South Park 06",
      X.show_name("South Park 06. série CZ DABING BEZ HESLA.zip"))
for show, season, want in [("South Park 06", 6, "South Park"), ("South Park 06", 5, "South Park 06"),
                           ("Babylon 5", 5, "Babylon 5"), ("Futurama", 8, "Futurama")]:
    check(f"drop_season_suffix {show!r} S{season} → {want!r}", X.drop_season_suffix(show, season) == want, X.drop_season_suffix(show, season))
os.makedirs("/downloads/serialy/Babylon"); check("seriál bez čísla v knihovně → číslo pryč", X.drop_season_suffix("Babylon 5", 5) == "Babylon")
os.rmdir("/downloads/serialy/Babylon")
check("UTF-8 názvy → názvy nechat", X.zip_is_cp852("a.zip", [{"path": "Červený trpaslík/S01E01 Žhavá.mkv"}]) is False)
check("ASCII názvy → názvy nechat", X.zip_is_cp852("a.zip", [{"path": "Futurama S08E01.mkv"}]) is False)
check("rar → názvy nechat", X.zip_is_cp852("a.rar", [{"path": "Žhavá".encode("cp852").decode("cp437")}]) is False)
eps = ["South Park 06 S06E08 - Žhavá katolická láska.mkv", "South Park 06 S06E09 - Zachraňte film a Čepičáře.mkv"]
os.makedirs(d, exist_ok=True)                                  # handle() po archivu Red Dwarf složku uklidil
arc = os.path.join(d, "South Park 06. série CZ DABING BEZ HESLA.zip")
raw = [e.encode("cp852") for e in eps]
holders = [f"{i}".ljust(len(b), "x") for i, b in enumerate(raw)]   # ASCII → zipfile nenastaví UTF-8; pak bajty nahradit
with zipfile.ZipFile(arc, "w") as z:
    for h in holders:
        z.writestr(h, "v" * 1000)
data = open(arc, "rb").read()
for h, b in zip(holders, raw):
    data = data.replace(h.encode(), b)
open(arc, "wb").write(data)
check("CP852 zip → rozbaleno", X.handle("sp", arc) == "done", st("sp"))
got = files("serialy")
check("CP852 zip → South Park/Season 06 a čeština v pořádku", got == [f"South Park/Season 06/{e}" for e in eps], got)
shutil.rmtree("/downloads/serialy")
# skutečný stav z Webshare: názvy uložené jako UTF-8, ale už převedené CP852 → CP437 („postiºen∞ch“)
eps = ["South Park 07 S07E02 - Šílený kriplové.mkv", "South Park 07 S07E09 - Křesťanský rock.mkv"]
os.makedirs(d, exist_ok=True)
arc = os.path.join(d, "South Park 07. série CZ DABING BEZ HESLA.zip")
with zipfile.ZipFile(arc, "w") as z:
    for e in eps:
        z.writestr("South Park 07/" + e.encode("cp852").decode("cp437"), "v" * 1000)
check("rozbité UTF-8 → rozbaleno", X.handle("sp7", arc) == "done", st("sp7"))
got = files("serialy")
check("rozbité UTF-8 → South Park/Season 07 a čeština v pořádku", got == [f"South Park/Season 07/{e}" for e in eps], got)
shutil.rmtree("/downloads/serialy")

# 6d) díly jen s číslem, série z názvu archivu (1. 11. 2026: „MĚSTEČKO SOUTH PARK 11. série (2007) CZ.rar“ → „01 Omluva….mkv“)
for t, want in [("MĚSTEČKO SOUTH PARK 11. série (2007) CZ.rar", 11), ("South Park 12. série CZ DABING BEZ HESLA.zip", 12),
                ("Futurama.S08.1080p.part1.rar", 8), ("Red Dwarf - Červený trpaslík 1-12 komplet.zip", None),
                ("Futurama.S01-S07.1080p.CZ.part1.rar", None), ("The Office (US) Season 1-9.7z.001", None), ("Film (2020).zip", None)]:
    check(f"archive_season {t!r} → {want}", X.archive_season(t) == want, X.archive_season(t))
for n in ("1080p.mkv", "2001 Vesmírná odysea.mkv", "Making of.avi"):
    check(f"{n!r} není díl jen s číslem", not X.EP_ONLY.match(n))
os.makedirs("/downloads/serialy/South Park/Season 10", exist_ok=True)      # seriál už v knihovně je
os.makedirs(d, exist_ok=True)
arc = os.path.join(d, "MĚSTEČKO SOUTH PARK 11. série (2007) CZ.rar".replace(".rar", ".zip"))
with zipfile.ZipFile(arc, "w") as z:
    z.writestr("01 Omluva Jesse Jacksonovi.mkv", "v" * 1000)
    z.writestr("10 Říše fantazie, část první.mkv", "v" * 1000)
    z.writestr("10 Říše fantazie, část první.srt", "t")
check("díly jen s číslem → rozbaleno", X.handle("sp11", arc) == "done", st("sp11"))
got = files("serialy")
check("díly jen s číslem → South Park/Season 11/South Park S11E01 - …",
      got == ["South Park/Season 11/South Park S11E01 - Omluva Jesse Jacksonovi.mkv",
              "South Park/Season 11/South Park S11E10 - Říše fantazie, část první.mkv",
              "South Park/Season 11/South Park S11E10 - Říše fantazie, část první.srt"], got)
check("díly jen s číslem → žádné nezařazené", "bez SxxEyy" not in st("sp11")["msg"], st("sp11")["msg"])
shutil.rmtree("/downloads/serialy")
os.makedirs(d, exist_ok=True)
with zipfile.ZipFile(os.path.join(d, "Neznamy serial 1-3 komplet.zip"), "w") as z:   # rozsah sérií → nehádat
    z.writestr("01 Pilot.mkv", "v" * 1000)
check("rozsah sérií → rozbaleno", X.handle("rng", os.path.join(d, "Neznamy serial 1-3 komplet.zip")) == "done", st("rng"))
check("rozsah sérií → díl zůstane v kořeni", files("serialy") == ["01 Pilot.mkv"], files("serialy"))
shutil.rmtree("/downloads/serialy")
# 6e) díl pod delším názvem seriálu, který už v knihovně je („Městečko South Park S14E01…“ → South Park)
for k in ("South Park", "Babylon"):
    os.makedirs(f"/downloads/serialy/{k}")
for n, want in [("Městečko South Park S14E01-14 2010 CZ dab 1080p - Léčba sexuální závislosti.mkv", "/South Park/Season 14"),
                ("Babylon 5 S01E01 Midnight.mkv", "/Babylon 5/Season 01"),          # „babylon5“ nekončí na „babylon“
                ("Futurama S08E01.mkv", "/Futurama/Season 08")]:
    check(f"knihovna: {n[:30]!r} → {want}", X.series_subdir(n) == want, X.series_subdir(n))
os.makedirs("/downloads/serialy/Park")                                      # dva kandidáti → nehádat
check("knihovna: víc kandidátů → název nechat", X.series_subdir("Městečko South Park S14E01.mkv") == "/Městečko South Park/Season 14",
      X.series_subdir("Městečko South Park S14E01.mkv"))
shutil.rmtree("/downloads/serialy")

# 8) někdo kouká v Jellyfinu → rozbalování počká; stará značka (app neběží) nezdržuje
import threading, time
X.HOLD_STEP = 0.1
arc = "Film (2020).zip"
d = put("filmy", arc)
open(X.PLAYING_FILE, "w").close()
res = {}
t = threading.Thread(target=lambda: res.setdefault("r", X.handle("hold1", os.path.join(d, arc)))); t.start()
time.sleep(0.5)
check("kouká se → archiv čeká (hold)", t.is_alive() and st("hold1").get("hold") and st("hold1")["state"] == "waiting", st("hold1"))
check("hláška o dokoukání", "dokouká" in st("hold1").get("msg", ""), st("hold1"))
os.remove(X.PLAYING_FILE); t.join(10)
check("dokoukáno → rozbalí", res.get("r") == "done" and not st("hold1").get("hold") and files("filmy"), (res, st("hold1")))
open(X.PLAYING_FILE, "w").close(); old = time.time() - X.PLAYING_FRESH - 5; os.utime(X.PLAYING_FILE, (old, old))
check("stará značka → nekouká se", X.playing_now() is False)
os.remove(X.PLAYING_FILE)
check("bez značky → nekouká se", X.playing_now() is False)
shutil.rmtree("/downloads/filmy", ignore_errors=True)

# 7) prune: zapomenuté gid pryč, běžící zůstává
X.set_state("run", state="running")
X.prune({"m"})
check("prune nechá známé a běžící", set(X.get_state()) == {"m", "run"}, set(X.get_state()))

print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
