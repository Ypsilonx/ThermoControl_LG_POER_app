# Podprojekt 5 – Zóny, režimy, program, dovolená, přebití

**Spec:** `docs/superpowers/specs/2026-09-30-zonove-rizeni-topeni-design.md` (kap. 2, 3, 5.1–5.2, 8–11)
**Rozhodnutí 2026-10-01 (varianta A):** zóny ovládají POER (cíl zóny + posun zařízení, fólie −1 °C)
a stávající regulace AC dostane cíl zóny. Nový přepínač režimů nahrazuje HAND/AUTO.
Výchozí je zkušební provoz (`dry_run`): jen deník rozhodnutí, nic se neposílá.

## Globální omezení

- Žádné nové závislosti; vše async, sqlite/soubory přes `asyncio.to_thread` jen kde blokuje.
- Všechny příkazy přes arbitra (`device_jobs.poer_command_job`, zdroj SCHEDULE/AUTOMATION/EMERGENCY).
- POER nikdy pod nouzové minimum (výchozí 12 °C).
- Šetřit API: POER stav max 1× za 2 min (sdílená cache), příkaz jen při změně cílové hodnoty
  zařízení; LG se kvůli zónám nečte navíc (stav z MQTT a z regulace).
- `data/zones.json`, `data/control.json` se necommitují (šablona `zones.json.example`).
- Volba zdroje, tarif, slunce, předpověď, vysoušení = podprojekt 6 (mimo rozsah).

## Moduly (`src/zones/`)

| Soubor | Odpovědnost |
|---|---|
| `config.py` | `zones.json` (zóny, topidla s posunem, čidla, role) a `control.json` (režim, program, automatika, dovolená, přebití, prahy, dry_run); výchozí hodnoty, validace, atomický zápis, migrace `state.json` HAND→manual / AUTO→automation |
| `sensors.py` | `Reading(value, ts, sensor_id)`, `resolve_role(sources, readings, now, max_age)` – první čerstvý zdroj; sběr hodnot ze zdrojů `poer`, `lg`, `chmi`, `http`, `smart_plug` (jen rozhraní) |
| `decide.py` | čisté funkce: blok programu (přes půlnoc a z neděle na pondělí), začátek dalšího bloku, noční útlum, dovolená s předtopením, přebití, nouzové minimum, krb → `ZoneDecision` |
| `loop.py` | řídicí smyčka 60 s: sběr čidel → rozhodnutí → deník → (mimo dry-run) POER příkazy a cíl pro AC; úklid prošlých přebití a konec dovolené |
| `web/routes/zones.py` | `/api/control/*`: stav zón, režim, konfigurace, zrušení přebití, deník |

## Úlohy

1. **Konfigurace** – `zones/config.py` + `data/zones.json.example` + `setup.py`, `.gitignore`,
   `.dockerignore`. Testy: výchozí hodnoty, validace (neznámý režim, špatný čas bloku, zóna bez
   topidla), migrace ze `state.json`, atomický zápis.
2. **Čidla** – `zones/sensors.py`. Testy: pořadí zdrojů, stáří (globální limit + `max_age_min`
   u čidla, ČHMÚ 240 min), offline POER bez hodnoty, LG s korekcí `proxy_offset`.
3. **Rozhodování** – `zones/decide.py`. Testy (scénáře): Ručně → bez cíle; Ručně + pod minimem →
   nouze; program – blok, přes půlnoc, neděle→pondělí; automatika + noční útlum přes půlnoc;
   dovolená před/během předtopení/po návratu; přebití v programu do dalšího bloku, v automatice
   N hodin; krb → pozastaveno (ne v Ručně); chybí vnitřní teplota → žádná změna.
4. **Smyčka** – `zones/loop.py` + napojení v `web/app.py` (start/stop, `app.state.zones`,
   LG stav z MQTT do cache, regulace AC bere cíl zóny mimo dry-run a neběží při pozastavení).
   Testy: dry-run nic nepošle; POER setpoint = cíl + posun, min. nouzové minimum, zaokrouhleno
   na 0,5; stejný setpoint se neposílá znovu; nouze → zdroj EMERGENCY; deník jen při změně.
5. **Přebití z ručních příkazů** – `routes/poer.py` set_temp a `routes/control.py`
   set_temperature v režimu ≠ Ručně založí přebití zóny daného zařízení.
6. **API** – `/api/control/` (GET stav), `PUT /mode`, `PUT /config`, `DELETE /override/{zona}`,
   `GET /journal`; `/api/mode` zůstává jako kompatibilní mapování (HAND ↔ manual,
   AUTO ↔ automation). `app.state.control_mode` se odvozuje (manual → HAND, jinak AUTO),
   takže HAND plánovač běží jen v Ručně a stará regulace AC v ostatních režimech.
7. **UI** – dashboard: přepínač 4 režimů místo AUTO/HAND, karty zón (teplota + zdroj, cíl,
   důvod, přebití se zbývajícím časem a „Zrušit“, štítek zkušebního provozu); nová stránka
   `/control` („Řízení“): Program (týdenní mřížka, kopírování dne), Automatika, Dovolená,
   Nastavení (nouzové minimum, délka přebití, stáří čidel, dry-run), Deník.
8. **Dokumentace a živé ověření** – CLAUDE.md/README, živě v dry-run (deník odpovídá),
   pak krátce mimo dry-run na koupelně a vrátit původní stav.

## Review focus

- Přechod přes půlnoc / neděle→pondělí v programu a nočním útlumu.
- Přebití založené ručním příkazem v režimu Ručně (nemá vzniknout).
- Dovolená bez `return_at` (režim nelze zapnout) a návrat po restartu serveru.
- Restart serveru: nepošle znovu stejné setpointy hned po startu víc než jednou.
- Všechny vnitřní teploty staré → zóna neposílá nic nového (spec 8).
