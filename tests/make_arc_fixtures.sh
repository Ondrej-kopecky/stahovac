#!/bin/bash
# Vyrobí testovací archivy pro test_extract.py do složky $1 (např. FX=$(mktemp -d); ./make_arc_fixtures.sh $FX).
# RAR umí vytvořit jen oficiální rar (trial od rarlab.com) — stáhne se do dočasné složky.
set -e
OUT=$(realpath "$1"); W=$(mktemp -d); cd "$W"
curl -sfL https://www.rarlab.com/rar/rarlinux-x64-712.tar.gz | tar xz
R=$W/rar/rar
mkdir -p src/nos src/movie "src/Futurama S08/Sample"
for e in 01 02 03; do
  head -c 3000000 /dev/urandom > "src/Futurama S08/Futurama.S08E$e.Nazev.dilu.1080p.mkv"
  printf '1\n00:00:01,000 --> 00:00:02,000\nahoj\n' > "src/Futurama S08/Futurama.S08E$e.Nazev.dilu.1080p.cz.srt"
done
head -c 500000 /dev/urandom > "src/Futurama S08/Sample/futurama.sample.mkv"
echo info > "src/Futurama S08/info.nfo"; echo x > "src/Futurama S08/Stahuj rychleji.exe"
for e in 01 02; do head -c 2000000 /dev/urandom > "src/nos/Show_01E${e}_Nazev.mkv"; done
head -c 4000000 /dev/urandom > "src/movie/Film (2020).mkv"
cd src
$R a -ma5 -v4m -m0 -idq "$OUT/Futurama.S08.1080p.rar" "Futurama S08"        # → .part1-3.rar
$R a -ma5 -v4m -m0 -idq "$OUT/Futurama.old.rar" "Futurama S08"              # → přejmenovat na .rar/.r00/.r01
$R a -ma5 -hpTajne123 -m0 -idq "$OUT/Heslo.hlav.rar" movie                  # heslo i na seznam souborů
$R a -ma5 -pTajne123 -m0 -idq "$OUT/Heslo.data.rar" movie                   # heslo jen na data
$R a -ma5 -m0 -idq "$OUT/Show.nos.rar" nos                                  # díly bez „S“
cd "$OUT"
mv Futurama.old.part1.rar Futurama.old.rar; mv Futurama.old.part2.rar Futurama.old.r00; mv Futurama.old.part3.rar Futurama.old.r01
python3 - "$W/src/movie/Film (2020).mkv" <<'EOF'
import sys, zipfile
with zipfile.ZipFile("Film (2020).zip", "w") as z:
    z.write(sys.argv[1], "Film (2020)/Film (2020).mkv"); z.writestr("Film (2020)/readme.txt", "x")
with zipfile.ZipFile("Zly.zip", "w") as z:                    # cesta ven
    z.writestr("../../ven.mkv", "x" * 100)
zi = zipfile.ZipInfo("odkaz.mkv"); zi.create_system = 3; zi.external_attr = 0o120777 << 16
with zipfile.ZipFile("Symlink.zip", "w") as z:                # symlink + zápis přes něj
    z.writestr(zi, "/etc/passwd"); z.writestr("odkaz.mkv/x.mkv", "y")
with zipfile.ZipFile("Bezvidea.zip", "w") as z:
    z.writestr("readme.txt", "x")
EOF
docker run --rm -v "$W/src:/s:ro" -v "$OUT:/o" --user "$(id -u):$(id -g)" --entrypoint 7z stahovac \
  a -v3m -mx0 -bd /o/Film7z.7z /s/movie >/dev/null                         # → .7z.001/.002
rm -rf "$W"; ls "$OUT"
