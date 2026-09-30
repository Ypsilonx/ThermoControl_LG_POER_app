
# ThermoControl-LG-POER_app

Webová a desktopová aplikace pro ovládání LG klimatizace a POER termostatu. Real-time MQTT push, plánování (HAND scheduler), sezónní automatika, ČHMÚ forecast a postupně i sjednocené řízení topení v místnosti. Primárně pro trvalé nasazení v domácí síti přes Docker.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Funkce

- **Webové rozhraní** – single-page dashboard (Tailwind CSS + Alpine.js), tmavý motiv, bez instalace klienta
- **Real-time push** – MQTT → WebSocket; stavové změny se promítají automaticky (indikátor "Push"/"Offline")
- **Ovládání klimatizace a POER termostatu** – LG má v dashboardu power, režimy (COOL/HEAT/FAN/AUTO/AIR_DRY), teplotu se sliderem + debounce (°C krok), větrání; ovládání polohy lamel není podporováno ThinQ Connect API. POER má ovládací panel ve web dashboardu i na stránce automatizace (cílová teplota, režim `AUTO`/`HEAT`/`OFF`, předvolba `HOME`/`AWAY`). Web dashboard má přepínací taby `LG klimatizace` / `POER termostat` pro ovládání (u POER přepínač mezi termostaty) a horní POER stavový indikátor.
- **AUTO / HAND** – AUTO = sezónní pravidla + PID regulace; HAND = ruční ovládání + HAND scheduler
- **HAND scheduler** – CRUD plánů: čas od/do, dny v týdnu, akce (mód/teplota/ventilátor), enable/disable
- **ČHMÚ forecast** – meteogram POI 510 (model ALADIN, asimiluje radar), horizont **72 h**, fallback na region RPZL; sjednocený **tmavý widget** na dashboardu i stránce automatizace: **vlevo aktuální počasí (vždy viditelné)** – velká ikona, teplota, slovní popis a detaily (vlhkost, oblačnost, srážky, vítr + směr `wind_dir_deg`, nárazy, tlak); **vpravo přepínací záložky Dny / Hodiny** – denní min/max teplota + srážky/oblačnost, hodinová předpověď s posuvníkem a horizontálním stripem podrobných kartiček; data jsou **automaticky obnovována** background taskem `_weather_refresh_loop` v intervalu `refresh_interval_hours` (výchozí **3 h**) – nezávisle na aktivním režimu (AUTO/HAND); první fetch proběhne okamžitě při startu serveru
- **Perzistence počasí** – poslední úspěšně stažená předpověď se ukládá do `data/weather_cache.json` (atomický zápis, přepisuje se při každé aktualizaci); při startu serveru se načte z disku, takže počasí je vidět **okamžitě po restartu** bez čekání na první fetch (základ pro budoucí plánovací automatiku)
- **Sezónní automatika** – zima/přechod/léto, blokace COOL mimo léto, PID-like regulace cílové teploty
- **Energy reporting** – den/týden/měsíc/rok s **procházením historie** (◀ / ▶ posun na starší období, popisek aktuálního rozsahu); **export do CSV** (UTF-8 s BOM, oddělovač `;` – přímo otevíratelné v Excelu) s podrobnými sloupci (raw i ISO datum, Wh, kWh, procentní podíl na období)
- **Docker** – `docker-compose up`, dostupné z domácí sítě, volitelný Cloudflare Tunnel
- **CLI** – `--mode cli --status`, `--list-devices`, `--command` pro smoke testy

## Architektura projektu

```
src/
├── main.py                # Vstupní bod (--mode web | cli)
├── server_api.py          # ThinQ API komunikace + MQTT klient
├── command_executor.py    # Sdílená logika provádění příkazů
├── command_policy.py      # Preconditions + plán kroků příkazů
├── klima_logic.py         # Payload generátor pro příkazy
├── energy_analytics.py    # Energy dotazy + CSV export
├── weather_provider.py    # ČHMÚ meteogram + regionální fallback
├── automation_rules.py    # Sezónní pravidla a blokace
├── thermal_controller.py  # PID-like regulace teploty
└── web/                   # Webová aplikace (primární)
    ├── app.py             # FastAPI instance, lifespan, MQTT→WS bridge
    ├── routes/
    │   ├── devices.py     # GET /api/devices/, /api/devices/{id}/status
    │   ├── control.py     # POST /api/devices/{id}/command
    │   ├── mode.py        # GET/POST /api/mode/  (AUTO ↔ HAND)
    │   ├── schedule.py    # CRUD /api/schedule/entries
    │   ├── energy.py      # GET /api/energy/{id} (view+offset), /{id}/export (CSV)
    │   ├── weather.py     # GET /api/weather/forecast, /config
    │   ├── poer.py        # GET /api/poer/status, POST /api/poer/command/*
    │   └── ws.py          # WebSocket /ws – real-time MQTT push
    ├── templates/         # Jinja2 šablony (Tailwind CDN + Alpine.js)
    └── static/

data/                      # Docker volume (necommitovat config.json, devices.json)
├── config.json            # API přihlašovací údaje ⚠️
├── devices.json           # Seznam zařízení ⚠️
├── device_profile.json    # Profil zařízení
├── schedule.json          # Časové plány HAND scheduleru
├── automation_rules.json  # Sezónní pravidla + weather (POI, indoor proxy, outdoor zdroj, interval, horizont)
└── weather_cache.json     # Poslední stažená předpověď ČHMÚ (autogenerovaná, necommitovat)

Dockerfile                 # python:3.12-slim, non-root uid 1000
docker-compose.yml         # lg-klimatizace + cloudflared (volitelné)
pyproject.toml             # definice projektu a závislostí (UV)
uv.lock                    # lockfile – přesné verze závislostí (commitovat!)
.python-version            # fixace Python verze pro UV
requirements.txt           # autogenerovaný z uv.lock (pip fallback)
```

## Rychlé spuštění

### Předpoklady

- Python 3.12+
- [UV](https://docs.astral.sh/uv/) – správce závislostí (doporučeno), nebo klasický pip
- LG ThinQ účet s registrovanými zařízeními
- LG Developer API přístup: <https://developer.lgaccount.com/>
  Získejte: `access_token`, `client_id`, `device_id`

### Instalace

**Doporučeno – UV** ([instalace UV](https://docs.astral.sh/uv/getting-started/installation/)):

```powershell
# Windows i Linux/macOS – UV vytvoří .venv a nainstaluje přesné verze z uv.lock
uv sync
uv run python setup.py   # vytvoří konfigurační soubory z šablon
```

**Alternativa – klasický pip** (bez UV):

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup.py
```

```bash
# Linux/macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python setup.py
```

> `requirements.txt` je autogenerovaný z `uv.lock` příkazem `uv export`. Oba soubory jsou synchronizované.

### Konfigurace

Projekt preferuje klíče v `.env` (lokálně i v Dockeru). Při spuštění přes
`uv run python ...` nebo `python ...` se `.env` načte automaticky.
Proměnné z prostředí shellu mají vždy vyšší prioritu.

**`data/config.json`:**
```json
{
    "access_token": "váš_access_token",
    "country_code": "CZ",
    "client_id": "váš_client_id"
}
```

**`data/devices.json`:**
```json
[{
    "deviceId": "vaše_device_id",
    "deviceInfo": {
        "deviceType": "DEVICE_AIR_CONDITIONER",
        "modelName": "LG AC Model",
        "alias": "Obývací pokoj",
        "reportable": true
    }
}]
```

Volitelně `data/automation_rules.json` – sezóny, weather provider (POI, region), korekce interního AC čidla `weather.ac_indoor_temperature_proxy_offset_c`, volitelná indoor teplota z externího termostatu `weather.indoor_current_temperature_c` a také externí zdroj aktuální venkovní teploty. Pro venkovní reálné čidlo použijte `weather.outdoor_current_temperature_url` s endpointem vracejícím např. `{"temperature_c": 12.3}`; pokud je URL dostupná, aplikace použije tuto hodnotu před fallbackem na předpověď.

> Pokud je vyplněno `weather.indoor_current_temperature_c`, regulace i dashboard používají tuto hodnotu jako prioritní indoor teplotu (místo odhadu z AC čidla).

> Korekce `ac_indoor_temperature_proxy_offset_c` se používá i pro skryté mapování cíle: při zadání pokojové teploty se do AC odešle korigovaný target (`ac_target = room_target - proxy_offset`). Příklad: offset `-2.0`, požadavek `24°C` -> do AC se odešle `26°C`.

### POER termostat bez Home Assistanta

Pro přímé čtení indoor teploty z POER cloudu:

1. Do `.env` nastavte `LG_POER_API_KEY` (hodnota z POER aplikace, včetně prefixu `cn` nebo `eu`).
2. V `data/automation_rules.json` nastavte:
    - `weather.indoor_current_temperature_source` na `"poer_api"`
    - volitelně `weather.poer_device_id` na konkrétní zařízení (jinak se použije první dostupné)

API key se používá pouze v runtime přes proměnné prostředí, neukládá se do JSON konfigurace.

POER je dostupný ve web dashboardu i na stránce automatizace (stav + ovládání mode/preset/teplota).

Web API endpointy pro POER:
- `GET /api/poer/status`
- `POST /api/poer/command/set-mode`
- `POST /api/poer/command/set-temperature`

> ⚠️ Nikdy necommitujte `config.json` ani `devices.json`!

### Spuštění

```powershell
# Web server (doporučeno) – přes UV (bez nutnosti aktivovat .venv)
uv run python src/main.py --mode web
# → http://localhost:8000    Swagger: http://localhost:8000/docs

# Nebo s aktivovaným .venv (klasicky)
python src/main.py --mode web

# CLI (smoke test)
uv run python src/main.py --mode cli --status

# Docker (trvalé nasazení)
docker-compose up -d
docker-compose logs -f lg-klimatizace
```

## HAND Scheduler – formát záznamu

```json
{
    "id": "uuid",
    "name": "Ranní chlazení",
    "enabled": true,
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "time_on": "07:30",
    "time_off": "09:00",
    "action": { "mode": "COOL", "temperature": 22.0, "wind_strength": "MID" }
}
```

`days: []` = každý den; `time_off: null` = bez automatického vypnutí;
`mode / temperature / wind_strength: null` = daný parametr neměnit.

## Příkazy API

| Kategorie | Příkaz | Hodnoty |
|-----------|--------|---------|
| Power | `power_on`, `power_off` | – |
| Režim | `change_mode` | `COOL` `HEAT` `FAN` `AUTO` `AIR_DRY` |
| Teplota | `set_temperature` | 16–30 °C |
| Větrání | `set_wind_strength` | `AUTO` `LOW` `MID` `HIGH` |
| Směr větru | `set_wind_direction` | nahoru/dolů, vlevo/vpravo |
| Lamely – kývání | `set_rotate_updown` | `true` / `false` |
| Lamely – kývání | `set_rotate_leftright` | `true` / `false` |
| Timer | `set_sleep_timer`, `cancel_all_timers` | minuty |

> ⚠️ ThinQ Connect API nepodporuje nastavení konkrétní polohy lamel – pouze boolean zapnutí/vypnutí kývání. Každá osa musí být odeslána jako **samostatný příkaz** (současné odeslání obou os způsobuje konflikt v zařízení).

## Bezpečnost

`.gitignore` a `.dockerignore` automaticky vylučují citlivé soubory:

| Commitovat | Necommitovat |
|------------|---------------|
| `data/*.example` | `data/config.json` |
| `src/`, `Dockerfile` | `data/devices.json` |
| `docker-compose.yml` | `data/schedule.json` |
| `pyproject.toml`, `uv.lock` | `data/weather_cache.json` |
| | `.venv/` |

- Token Cloudflare Tunnel ukládejte jako systémovou proměnnou (`CLOUDFLARE_TUNNEL_TOKEN`), nikdy do souborů.
- Rotujte API klíče pravidelně.

## Řešení problémů

| Chyba | Řešení |
|-------|--------|
| `FileNotFoundError: config.json` | Spusťte `uv run python setup.py` nebo `python setup.py` |
| `401 / 403` | Zkontrolujte `data/config.json`, ověřte PAT v LG Developer portálu |
| `503 Service Unavailable` | LG API dočasně nedostupné; aplikace opakuje automaticky (3×) |
| `NOT_PROVIDED_FEATURE` | Funkce není modelem podporována – viz `device_profile.json` |
| Rozbité české znaky (Linux) | `export LANG=cs_CZ.UTF-8` |

## Dokumentace

- Pořadí příkazů, preconditions, retry policy: `docs/command-order-playbook.md`
- Implementované fáze (automatika, weather, scheduler, web): `docs/scheduler-weather-roadmap.md`
- Přispívání: `CONTRIBUTING.md`
