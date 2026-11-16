# Stahovač: z Webshare rovnou do Jellyfinu

Webová stránka na domácím serveru (nebo PC). Vložíš do ní odkaz z Webshare a server film
stáhne šestnácti spojeními najednou rovnou do knihovny Jellyfinu. Do pár sekund je v televizi.

- **Vložit odkaz** z Webshare nebo Fastshare, víc odkazů naráz, z počítače i z telefonu
- **Objevit:** novinky a nejlépe hodnocené filmy a seriály z TMDB v češtině; u filmu stahovač sám
  najde na Webshare nejlepší verzi (čeština, pak 4K, 1080p, pak dabing před titulky)
- **Wishlist:** co ještě není v dobré kvalitě, hlídá každých 6 hodin
- **Knihovna:** co je stažené, rozkoukané a zhlédnuté, smazání jedním klikem (zmizí i z Jellyfinu)
- **Archivy** (rar i po částech, zip, 7z, s heslem) rozbalí sám, **seriály** roztřídí do `Seriál/Season 01`
- Když se v Jellyfinu kouká, stahování zpomalí, ať se film nesekal

Podrobný popis, jak to celé funguje: <https://ongy.cz/pages/server/kino/>

> Stahovač řeší techniku, ne obsah. Stahuj jen to, k čemu máš práva.

## Co budeš potřebovat

- **Docker** s Docker Compose. Na serveru (Linux, Proxmox) nebo na PC přes
  [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, Mac).
- **Účet Webshare.** Bez VIP to jde, ale hodně pomalu.
- **Jellyfin.** Buď už ho máš, nebo ho spustíš rovnou s tímhle (viz níže).
- Volitelně klíč k TMDB (zdarma) pro stránku Objevit.

## Spuštění

```bash
git clone https://github.com/Ondrej-kopecky/stahovac.git
cd stahovac
cp .env.example .env
nano .env                                  # vyplň aspoň WS_USER, WS_PASS, WEB_PASS a MEDIA_DIR
docker compose up -d --build               # jen stahovač (Jellyfin už máš)
docker compose --profile jellyfin up -d --build   # stahovač + Jellyfin
```

Stránka pak běží na `http://<BIND_IP>:8097` (výchozí `http://127.0.0.1:8097`, jen z toho počítače).
Aby šla otevřít z telefonu, nastav v `.env` `BIND_IP` na adresu počítače v domácí síti.

### Jellyfin

V Jellyfinu přidej dvě knihovny:

| Knihovna | Typ | Složka (jak ji vidí Jellyfin) |
|---|---|---|
| Filmy | Filmy | `<JELLYFIN_MEDIA_PATH>/filmy` |
| Seriály | Seriály | `<JELLYFIN_MEDIA_PATH>/serialy` |

Pak v Jellyfinu vytvoř klíč API (Ovládací panel → Klíče API) a dej ho do `.env` jako `JELLYFIN_TOKEN`
spolu s `JELLYFIN_URL`. Díky tomu se nový film objeví do sekundy a stránka ukáže plakáty a co je zhlédnuté.

Nejdůležitější je, aby stahovač i Jellyfin viděly **stejnou složku**. Stahovač do ní zapisuje
jako `MEDIA_DIR`, Jellyfin ji čte pod cestou `JELLYFIN_MEDIA_PATH`.

### Na PC bez serveru

Jde to i na obyčejném počítači s Docker Desktopem. Do `.env` dej třeba `MEDIA_DIR=C:/Media`
a spusť variantu s Jellyfinem. Televize si pak filmy přehrává z PC, ale jen když je PC zapnuté.
Stahuje se taky jen se zapnutým PC.

### Proxmox

Pro začátek je nejjednodušší jeden virtuál (nebo LXC) s Dockerem, ve kterém poběží obojí
(`--profile jellyfin`). Když už máš Jellyfin v samostatném LXC, musí mít oba kontejnery
připojenou stejnou složku z hostitele (bind mount `mp0` v Proxmoxu). V `.env` pak
`JELLYFIN_URL` míří na adresu toho LXC a `JELLYFIN_MEDIA_PATH` je cesta k té složce uvnitř LXC s Jellyfinem.

## Bezpečnost

Stránka umí mazat soubory, proto:

- **Nikdy ji nevystavuj do internetu.** Mimo domov přes VPN (třeba Tailscale), ne přes přesměrování portu.
- Bez `WEB_PASS` stránka nic neukáže. Po pěti špatných heslech je přihlášení na 15 minut zablokované.
- Mazat jde jen uvnitř `filmy/` a `serialy/`, přímé odkazy do domácí sítě stahovač odmítne.
- Archivy před rozbalením kontroluje (cesty ven, symbolické odkazy, místo na disku).
- Hesla jsou jen v `.env`. Ten nikomu neposílej.

## Jak to funguje

FastAPI a [aria2](https://aria2.github.io/) v jednom kontejneru. Stahovač se přihlásí k Webshare API,
dostane přímý odkaz a předá ho aria2, které soubor stahuje 16 spojeními do skryté složky
`.incomplete` (Jellyfin rozpracovaný soubor nevidí). Po dokončení ho skript přesune na místo
a řekne Jellyfinu, ať prohledá knihovnu.

| Soubor | Co dělá |
|---|---|
| `app/app.py` | web, přihlášení, Webshare a Fastshare, aria2, knihovna |
| `app/discover.py` | TMDB, hodnocení verzí podle názvu souboru, wishlist |
| `app/extract.py` | rozbalování archivů, třídění seriálů, sken Jellyfinu |
| `app/on_complete.py` | po stažení: přesun do knihovny |
| `app/static/` | stránka (jeden HTML soubor) |

## Testy

```bash
docker compose build stahovac
docker run --rm --entrypoint python -v "$PWD/tests:/t:ro" stahovac /t/test_discover.py
docker run --rm --entrypoint python -v "$PWD/tests:/t:ro" stahovac /t/test_fixes.py
```

Rozbalování (`tests/test_extract.py`) potřebuje testovací archivy, návod je v hlavičce souboru.

## Licence

MIT, viz [LICENSE](LICENSE). Bez záruky: je to domácí projekt, ne hotový produkt.
