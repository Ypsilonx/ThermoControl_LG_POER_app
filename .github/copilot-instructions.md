# GitHub Copilot – instrukce pro workspace

## Projekt
**Typ:** FastAPI webová aplikace + CLI  
**Účel:** Ovládání LG ThinQ klimatizací přes domácí síť – real-time MQTT push, plánování (HAND scheduler), sezónní automatika, ČHMÚ počasí, energy reporting.  
**Primární nasazení:** Docker na domácím serveru, přístup přes prohlížeč. CLI pro jednorázové příkazy.  
**Cílový OS:** Windows (vývoj), Linux (Docker produkce)  
**Python:** 3.12+, venv v `.venv/`

## Architektura

```
src/
├── main.py                # Vstupní bod: --mode web (výchozí) | cli
├── server_api.py          # ThinQ HTTP API + MQTT klient (thinqconnect)
├── command_executor.py    # Provádění příkazů (sdílená logika)
├── command_policy.py      # Preconditions + plán kroků
├── klima_logic.py         # Payload generátor pro AC příkazy
├── energy_analytics.py    # Energy dotazy + CSV export
├── weather_provider.py    # ČHMÚ meteogram + regionální fallback
├── automation_rules.py    # Sezónní pravidla a blokace módů
├── thermal_controller.py  # PID-like regulace teploty
└── web/                   # Primární webová aplikace
    ├── app.py             # FastAPI instance, lifespan, MQTT→WS bridge
    └── routes/            # REST endpointy + WebSocket
```

## Klíčové konvence

- **Jazyk kódu i komentářů:** čeština
- **Dokumentace:** každá funkce/třída má docstring (účel, parametry, návratová hodnota)
- **Modularita:** nepiš monolitické bloky; rozčleňuj do logických modulů
- **Závislosti:** minimalizuj externí knihovny; preferuj stdlib
- **Flake8:** max-line-length=100; konfigurace v `.flake8`
- **Intra-project importy:** bare imports (`from server_api import ...`), `sys.path` se nastavuje v `main.py`
- **Async:** aplikace je `async`/`await` – neblokuj event loop synchronními voláními
- **Data soubory:** konfigurace v `data/` (JSON), příklady v `*.json.example`

## Důležité upozornění

- `thinqconnect`, `awscrt`, `awsiotsdk` vyžadují kompilaci C – **nelze lehce instalovat v CI**; lint job záměrně neinstaluje `requirements.txt`
- Jediné uživatelské rozhraní je web (`--mode web`); Tkinter GUI bylo odstraněno
- `except Exception as e` + lambda: vždy zachytávat hodnotou `lambda err=e: ...`, ne referencí
