# Nastavení zón a režimů řízení

Aplikace řídí topení po **zónách**. Zóna je místnost nebo část domu s vlastní cílovou
teplotou. Každá instalace má jiný dům, proto se zóny nastavují **v aplikaci**. Repozitář
obsahuje jen obecnou šablonu, žádné konkrétní nastavení.

## První spuštění

1. Vyplňte přístupy (`.env`, `data/config.json`, `data/devices.json`), viz README.
2. Otevřete stránku **Řízení** (`/control`), záložka **🏠 Dům**. Bez nastavených zón se otevře sama
   a dashboard na ni odkazuje.
3. Klikněte na **🔍 Navrhnout z nalezených zařízení**. Aplikace najde:
   - POER termostaty na účtu (podle `LG_POER_API_KEY`) – každý dostane vlastní zónu
     pojmenovanou podle názvu v aplikaci POER,
   - klimatizace z `data/devices.json` – přidají se do první zóny.
4. Návrh upravte (názvy, topidla, posuny, pořadí čidel) a klikněte na **Uložit zóny**.
   Řízení nové nastavení použije hned, bez restartu.
5. V záložkách Program / Automatika / Dovolená nastavte teploty a v **Nastavení** nechte
   zapnutý **🧪 zkušební provoz**, dokud deník rozhodnutí neodpovídá vašemu očekávání.

## Pojmy

| Pojem | Význam |
|---|---|
| **Topidlo** | Zařízení, které zóna ovládá. `poer:<id>` = termostat POER, `lg:<id>` = konkrétní klimatizace, `lg:*` = všechny klimatizace. |
| **Posun** (`offset_c`) | O kolik se setpoint termostatu liší od cíle zóny. Např. podlahová fólie jako základ topení: `-1` → při cíli 21 °C dostane termostat 20 °C. |
| **Čidlo** | Zdroj hodnot: `poer` (teplota a vlhkost termostatu), `lg` (vnitřní čidlo klimatizace s korekcí), `chmi` (ČHMÚ), `http` (vlastní čidlo), `smart_plug` (zásuvka čerpadla krbu – zatím jen rozhraní). |
| **Role** | K čemu zóna čidlo používá: vnitřní teplota, vnitřní vlhkost, venkovní teplota, krb topí. Pro každou roli je **seřazený seznam** čidel – použije se první, které má čerstvou hodnotu (výchozí limit 15 min, ČHMÚ 240 min). |
| **Strana** (`sun_side`) | `east`/`west` – na kterou stranu domu zóna leží; podle ní Automatika snižuje cíl, když tam svítí slunce. |
| **Nouzové minimum** | Pod tuto teplotu (výchozí 12 °C) zóna topí v každém režimu, i v Ručně. Termostaty POER nikdy nedostanou nižší cíl. |

## Režimy

| Režim | Cíl zóny |
|---|---|
| **Ručně** | Aplikace neřídí (jen nouzové minimum). Běží HAND plánovač klimatizace. |
| **Program** | Týdenní bloky Po–Ne; blok platí do začátku dalšího. |
| **Automatika** | Cíl pro každou zónu + volitelný noční útlum. |
| **Dovolená** | Teploty nepřítomnosti, N hodin před návratem předtopení, pak návrat do předchozího režimu. |

Ruční změna teploty na webu v režimu Program/Automatika/Dovolená je **dočasné přebití** zóny
(v Programu do dalšího bloku, jinak na nastavitelnou dobu, výchozí 2 h). Zrušit jde tlačítkem
na kartě zóny.

### Úpravy cíle v Automatice

- **☀️ Slunce** – zóna na východ: od 2 h před východem slunce do poledne; na západ: od poledne do
  západu. Při průměrné oblačnosti v předpovědi pod 40 % se cíl sníží o 0,5 °C. Východ a západ slunce
  se počítají ze souřadnic domu (Nastavení).
- **💧 Vysoušení** – vlhkost nad 70 % → cíl +1 °C nejdéle 60 min; znovu až po poklesu pod 65 %.

Všechny hodnoty se mění v záložce Automatika.

## Tarif a volba zdroje tepla

**Okna nízkého tarifu (NT)** se zadávají v Nastavení zvlášť pro pracovní den a víkend (výchozí
orientačně 22:00–06:00; přepište podle HDO svého distributora).

V zóně, kde je **klimatizace i POER topidlo** (typicky podlahová fólie), je klimatizace hlavní zdroj:

| Situace | Fólie (POER) |
|---|---|
| NT | základ: cíl + posun topidla (např. −1 °C) |
| NT + předpověď mrazu (do 12 h pod 0 °C) | plný cíl – nahřívá do zásoby |
| VT | vypnuto (nouzové minimum), topí klimatizace |
| VT a klimatizace nedostupná / nestačí (topí 30 min a teplota přesto klesla o 0,3 °C) / venku pod −5 °C | plný cíl – záloha |
| pod nouzovým minimem | topí vždy |

Zóna **jen s POER topidlem** (např. koupelna s kabelem) topí na cíl vždy.

V **chladicí sezóně** (léto podle `automation_rules.json`) drží všechna POER topidla nouzové minimum – chladí jen klimatizace. Proč topidlo
právě tak topí, ukazuje karta zóny na dashboardu a deník rozhodnutí.

**Zkušební provoz** (výchozí): řízení počítá a zapisuje deník, ale **nic neposílá** – ani
termostatům, ani klimatizaci.

## Kde jsou data uložená

Všechno je v adresáři `data/` (v Dockeru volume), **necommituje se** (`.gitignore`, `.dockerignore`):

| Soubor | Obsah | Kdo ho mění |
|---|---|---|
| `data/zones.json` | Zóny, topidla, čidla, role | Záložka 🏠 Dům |
| `data/control.json` | Režim, program, automatika, dovolená, přebití, tarif, prahy, souřadnice | Ostatní záložky a dashboard |

Pro přenos na jiný server (např. z vývojového počítače do Dockeru) stačí oba soubory zkopírovat
do `data/` – nemusíte nic proklikávat znovu. Když `control.json` chybí, vytvoří se s výchozím
programem (a režim převezme ze staršího `state.json`: HAND → Ručně, AUTO → Automatika).

## Ruční úprava `zones.json`

Není potřeba, ale je možná. Formát ukazuje šablona `data/zones.json.example`:

```json
{
  "zones": {
    "kuchoobyvak": {
      "name": "Kuchoobývák",
      "sun_side": "east",
      "heaters": [{"device": "lg:*"}, {"device": "poer:ID_TERMOSTATU", "offset_c": -1.0}],
      "roles": {
        "indoor_temperature": ["poer_kuchoobyvak", "lg_klima"],
        "outdoor_temperature": ["chmi"],
        "fireplace": ["zasuvka_cerpadlo"]
      }
    }
  },
  "sensors": {
    "poer_kuchoobyvak": {"source": "poer", "device_id": "ID_TERMOSTATU"},
    "lg_klima": {"source": "lg"},
    "chmi": {"source": "chmi", "max_age_min": 240},
    "cidlo_vychod": {"source": "http", "url": "http://192.168.1.50/teplota"},
    "zasuvka_cerpadlo": {"source": "smart_plug"}
  }
}
```

- ID zón a čidel: jen malá písmena bez diakritiky, číslice a `_`.
- Čidlo `http` musí vracet JSON `{"temperature_c": 12.3}` (volitelně `"humidity_pct"`).
- ID termostatů POER najdete v editoru (nebo `GET /api/poer/devices`).
- Po ruční úpravě restartujte server; neplatný soubor se v logu ohlásí a zónové řízení neběží.
