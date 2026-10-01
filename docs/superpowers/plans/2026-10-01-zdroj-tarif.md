# Podprojekt 6 – Automatika a volba zdroje tepla

**Spec:** `docs/superpowers/specs/2026-09-30-zonove-rizeni-topeni-design.md` (kap. 2.2, 6, 7)
**Navazuje na:** podprojekt 5 (`src/zones/`).

## Rozhodnutí (2026-10-01, vše nastavitelné v aplikaci)

- Tarif NT: okna `HH:MM–HH:MM` zvlášť pro pracovní den a víkend; výchozí 22:00–06:00.
- Zóna s klimatizací: POER topidla v ní jsou fólie.
  - NT: setpoint = cíl + posun (−1 °C); předpověď mrazu (min. teplota do 12 h < 0 °C) → plný cíl.
  - VT: nouzové minimum, ledaže je potřeba záloha: AC nestačí (topí ≥ 30 min a vnitřní teplota
    za 30 min klesla o ≥ 0,3 °C pod cílem), AC nedostupná (zdraví v arbitrovi), venku < −5 °C,
    nebo nouze → plný cíl.
- Zóna bez klimatizace (koupelna): setpoint = cíl + posun vždy.
- Slunce (jen Automatika): zóna má `sun_side` (`east`/`west`); okno east = východ slunce − 2 h
  až 12:00, west = 12:00 až západ; průměrná oblačnost v okně < 40 % → cíl − 0,5 °C.
  Východ/západ slunce výpočtem (NOAA), souřadnice v nastavení (výchozí Valašské Meziříčí).
- Vysoušení (jen Automatika): vlhkost > 70 % → cíl + 1 °C nejdéle 60 min, znovu až pod 65 %.
- Plynulá změna cíle AC se samostatně neřeší (regulace má cooldown 5 min).

## Moduly

| Soubor | Odpovědnost |
|---|---|
| `zones/tariff.py` | `is_low_tariff(tariff, now)` – okna přes půlnoc, pracovní den/víkend |
| `zones/sun.py` | `sun_times(day, lat, lon)` – východ/západ slunce v místním čase |
| `zones/sources.py` | čisté funkce: úpravy cíle (slunce, vysoušení), mráz, setpoint fólie/kabelu |
| `zones/config.py` | sekce `sources` v control.json (tarif, prahy, slunce, vysoušení, souřadnice), `sun_side` v zones.json |
| `zones/loop.py` | kontext (tarif, předpověď, zdraví AC, trend teploty, stav AC) → setpointy + důvody v deníku |
| UI | Nastavení: okna NT, prahy, souřadnice; Automatika: přepínače slunce/mráz/vysoušení; Dům: strana zóny |

## Úlohy

1. Tarif + slunce (čisté funkce, testy: přes půlnoc, víkend, východ/západ ±10 min proti tabulce).
2. Konfigurace `sources` + `sun_side` (výchozí hodnoty, validace, migrace starých souborů doplněním).
3. Volba zdroje a úpravy cíle (scénářové testy: NT/VT, mráz, AC nestačí, AC nedostupná, venku pod
   prahem, nouze, kabel; slunce jasno/zataženo, vysoušení s časovým limitem a hysterezí).
4. Napojení do smyčky (trend teploty, stav AC z LG, zdraví z arbitra, předpověď z cache počasí).
5. UI a dokumentace (`docs/nastaveni-zon.md`, CLAUDE.md), živé ověření v zkušebním provozu.

## Review focus

- Okno NT přes půlnoc a přechod pátek → sobota.
- Chybějící předpověď nebo venkovní teplota → žádná úprava ani záloha z venkovní teploty.
- Restart serveru: trend teploty prázdný → „AC nestačí“ nesmí falešně sepnout.
- Vysoušení se nesmí zapínat dokola, když vlhkost zůstává vysoko.
