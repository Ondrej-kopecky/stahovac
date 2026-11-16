#!/usr/local/bin/python3
"""
aria2 hook (--on-download-complete): args = gid, počet souborů, cesta k souboru.

1) Soubor se stahoval do /downloads/.incomplete/<cíl>/... (Jellyfin tuhle složku nevidí,
   takže nikdy neindexuje rozpracovaný soubor). Po dokončení ho přesuneme na finální místo.
2) Řekneme Jellyfinu přes API, že v dané složce přibyl soubor → sken jen té složky,
   film/epizoda se v knihovně objeví do pár sekund (místo čekání na plánovaný sken).
3) Archiv (.rar/.zip/.7z, i po částech) zůstane v .incomplete a rozbalí ho extract.py.
"""
import os
import shutil
import signal
import sys

import extract
from extract import INCOMPLETE, free_name, jf_refresh

INCOMPLETE_ = INCOMPLETE + "/"


def log(msg):
    print(f"[on_complete] {msg}", flush=True)


def main():
    # aria2 spouští hook s ignorovaným SIGCHLD → subprocess by u 7z vždy viděl návratový kód 0
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    if len(sys.argv) < 4:
        return
    gid, path = sys.argv[1], sys.argv[3]
    if not path.startswith(INCOMPLETE_):
        log(f"cesta mimo .incomplete, nechávám: {path}")
        return

    if extract.is_archive(path):
        # .aria2 se po dokončení maže sám, ale pro jistotu — podle něj extract pozná, že díl ještě běží
        if os.path.exists(path + ".aria2"):
            os.remove(path + ".aria2")
        log(f"archiv {os.path.basename(path)}: {extract.handle(gid, path)}")
        return

    rel = path[len(INCOMPLETE_):]                    # filmy/Nazev.mkv | serialy/Show/Season 01/x.mkv
    dest_dir = os.path.join("/downloads", os.path.dirname(rel))
    os.makedirs(dest_dir, exist_ok=True)
    dest = free_name(os.path.join(dest_dir, os.path.basename(path)))
    shutil.move(path, dest)
    for leftover in (path + ".aria2",):
        if os.path.exists(leftover):
            os.remove(leftover)
    log(f"přesunuto → {dest}")

    # Uklidit prázdné podsložky v .incomplete
    d = os.path.dirname(path)
    while d.startswith(INCOMPLETE) and d != INCOMPLETE:
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)

    jf_refresh(rel)


if __name__ == "__main__":
    main()
