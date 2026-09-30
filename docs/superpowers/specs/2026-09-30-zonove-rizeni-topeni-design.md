# Zónové řízení topení (LG + POER) – návrh

Datum: 2026-09-30
Stav: schváleno v diskusi, čeká na revizi specifikace

## 1. Cíl a kontext

Sjednotit řízení klimatizace LG a dvou termostatů POER do jednoho systému, který:

- odstraní kolize příkazů (dnes posílají příkazy web, HAND scheduler i AUTO smyčka bez koordinace),
- řídí topení po zónách podle týdenního programu nebo plně automaticky,
- respektuje tarif D25d, krbová kamna a otevřená okna,
- je připravený na budoucí venkovní čidla (východ/západ) a chytrou zásuvku čerpadla radiátoru.

Priorita je **topení** – nedotop je horší než přetop, starý dům má přebytek tepla zřídka.

### Fyzická realita domu

| Zóna | Strana | Ovládané zdroje | Neovládané zdroje |
|---|---|---|---|
| Kuchoobývák (kuchyň + obývák) | východ | LG klimatizace (tepelné čerpadlo, COP ~3), POER + podlahová fólie 3,5 kW | radiátor krbových kamen s výměníkem |
| Koupelna | západ | POER + podlahový kabel 560 W | – |

- Tarif **D25d** (NT ~8 h/den, řízeno HDO) – časy NT zatím neznámé, musí být konfigurovatelné v aplikaci.
- Venkovní data: dnes jen ČHMÚ (hvězdárna Valašské Meziříčí, meteogram + předpověď). Budoucí čidla východ/západ přes HTTP.
- POER termostaty budou v ručním režimu na straně POER cloudu – jejich vlastní týdenní program se nepoužívá, řídí je jen tato aplikace.

## 2. Režimy řízení

Globální přepínač (vnitřně má režim každá zóna, přepínač nastavuje všechny najednou – umožní budoucí přechod na režim per zóna bez přepisu):

| Režim | Uživatel nastavuje | Cílová teplota zóny |
|---|---|---|
| **Ručně** | přímo zařízení | nepočítá se, platí jen nouzové minimum |
| **Program** | týdenní cyklus Po–Ne | z aktuálního bloku programu |
| **Automatika** | cíl pro každou zónu + volitelný noční útlum | cíl upravený o slunce, předpověď, vlhkost |
| **Dovolená** | termín návratu, teploty nepřítomnosti, předtopení | teplota nepřítomnosti, před návratem komfort |

### 2.1 Program

- Pro každý den Po–Ne seznam bloků `{od: "HH:MM", cíle: {zóna: °C}}`. Blok platí do začátku dalšího; po neděli pokračuje pondělí.
- UI umožní zkopírovat den na jiné dny.

### 2.2 Automatika

- Parametry: cílová teplota pro každou zónu, volitelný noční útlum (od–do, o kolik °C), přepínače úprav níže.
- Úpravy cíle:
  - **Slunce** – souřadnice domu (výchozí Valašské Meziříčí 49.47 N, 17.97 E), východ/západ slunce počítaný bez externí knihovny. Při oblačnosti z předpovědi < 40 % a < 2 h před východem slunce se cíl kuchoobýváku sníží o 0,5–1 °C; analogicky koupelna odpoledne před západem.
  - **Předpověď** – při očekávaném nočním mrazu se fólie v NT nahřívá do zásoby (vyšší cíl pro fólii).
  - **Vysoušení koupelny** – vlhkost > 70 % → kabel topí i nad cíl po omezenou dobu (proti plísni). Vypínatelné.

### 2.3 Dovolená

- Termín návratu (datum + čas), teploty nepřítomnosti pro každou zónu (výchozí kuchoobývák 15 °C, koupelna 16 °C).
- Předtopení: N hodin před návratem (výchozí 6 h) se cíl vrací na komfort.
- Po termínu se aplikace vrátí do režimu, který byl aktivní před dovolenou.

### 2.4 Dočasné přebití

Ruční změna v režimu Program/Automatika/Dovolená je **dočasné přebití** zóny:

- v Programu do začátku dalšího bloku,
- v Automatice a Dovolené na nastavitelnou dobu (výchozí 2 h),
- tlačítko „Zrušit přebití“ ho ukončí okamžitě.

Nahrazuje dnešní `manual_override_requires_resume_button`.

## 3. Priority rozhodování zóny

Od nejvyšší:

1. **Nouzové minimum** (výchozí 12 °C) – topí se vždy, ve všech režimech včetně Ručně.
2. **Pojistky** (neplatí v Ručně) – krb topí nebo otevřené okno → zóna pozastavena.
3. **Cílová teplota** podle režimu (případně přebití).
4. **Volba zdroje** (kapitola 6).

## 4. Zařízení a arbitr příkazů

### 4.1 Jednotné rozhraní zařízení

Každé ovládané zařízení: *přečti stav*, *nastav cílovou teplotu*, *zapni/vypni*.

- **LG klimatizace** – obal nad stávající pipeline `build_command_plan` → `execute_plan` (preconditions zůstávají v `command_policy.py`).
- **POER termostat** – obal nad `poer_api.py`, rozšířeným na **více termostatů**:
  - dnes `fetch_poer_status` vezme `preferred_device_id` nebo `devices[0]` a zbytek seznamu ze `SYNC` zahodí – nutno vracet všechna zařízení,
  - stav všech termostatů jedním `QUERY` (přijímá seznam ID),
  - příkazy adresované konkrétním `device_id`,
  - „vypnutí“ = nastavení na nouzové minimum, ne úplné vypnutí (viz 8).

### 4.2 Arbitr příkazů

- **Jedna fronta na zařízení**, příkazy se vykonávají sériově – vícekrokový plán (zapni → mód → teplota) nelze proložit.
- Každý příkaz nese zdroj: `nouze` > `ručně` / `přebití` > `program` / `automatika`.
- Novější čekající příkaz ze stejného zdroje nahradí starší.
- Příkaz, který nemění stav zařízení, se nevykoná.
- Omezení frekvence na zařízení (výchozí LG 30 s, POER 2 min; příkazy `nouze` a `ručně` limit obcházejí).
- Selhání → opakování s rostoucí prodlevou; po opakovaném selhání je zařízení označeno jako nedostupné a zóna přejde na jiný zdroj.
- **Všechny cesty jdou přes arbitra**: `routes/control.py`, `routes/poer.py`, řídicí smyčka, CLI. Přímá volání `execute_plan` / `send_poer_command` mimo arbitra zaniknou.

Arbitr je nasaditelný samostatně – odstraní kolize ještě před zavedením zón.

## 5. Čidla a signály

### 5.1 Registr čidel

Čidlo: `id`, umístění, veličina (teplota / vlhkost / binární), zdroj:

- `poer` – vnitřní teplota a vlhkost konkrétního termostatu,
- `lg` – vnitřní teplota klimatizace s korekcí `ac_indoor_temperature_proxy_offset_c`,
- `chmi` – aktuální hodnota a předpověď ČHMÚ,
- `http` – obecná URL vracející JSON (budoucí čidla východ/západ),
- `smart_plug` – pouze rozhraní pro indikátor čerpadla, konkrétní implementace později.

### 5.2 Role v zóně

Zóna odkazuje role na **seřazený seznam zdrojů**:

```
kuchoobyvak.vnitrni_teplota  = [poer_kuchoobyvak, lg_klima]
kuchoobyvak.venkovni_teplota = [cidlo_vychod, chmi]
kuchoobyvak.indikator_krbu   = [zasuvka_cerpadlo]
koupelna.vnitrni_teplota     = [poer_koupelna]
koupelna.vnitrni_vlhkost     = [poer_koupelna]
koupelna.venkovni_teplota    = [cidlo_zapad, chmi]
```

Každá hodnota nese čas měření; starší než limit (výchozí 15 min) → použije se další zdroj. UI ukazuje aktuálně použitý zdroj.

### 5.3 Odvozené signály

- **Krb topí** – pouze z chytré zásuvky čerpadla radiátoru (rozhodnutí 2026-09-30). Odhad z trendu teploty se nedělá; bez zásuvky automatika o krbu neví a topení odstaví regulace po dosažení cíle.
- Detekce otevřeného okna se nedělá (rozhodnutí 2026-09-30).

### 5.4 Historie dat

Pro ladění automatiky se ukládá historie do SQLite `data/history.db` (výchozí uchování 90 dní):

- `measurements` (úzká tabulka čas/zdroj/zařízení/veličina/hodnota/text): POER oba termostaty každých 5 min (teplota, vlhkost, cíl, režim, topí), LG z MQTT zpráv (zapnuto, režim, cíl, teplota), ČHMÚ při každém stažení (teplota, oblačnost, srážky, vlhkost, vítr).
- `forecast`: snímek hodinové předpovědi při každém stažení (porovnání předpověď × skutečnost).
- `energy_daily`: denní spotřeba AC z LG (1 volání/den).
- Intervaly topení se neukládají, počítají se z `heating` při exportu (odhad kWh pro POER podle příkonu).
- Export CSV: `GET /api/history/export`. Grafy až po nasbírání dat.

## 6. Volba zdroje tepla

### Kuchoobývák

1. Krb topí → pozastaveno (AC i fólie netopí).
2. **Klimatizace** – hlavní zdroj, topí k cíli.
3. **Fólie**:
   - v **NT** drží základ `cíl − 1 °C` (+ nahřívání do zásoby při předpovědi mrazu),
   - ve **VT** jen jako záloha: AC nestačí (teplota klesá přestože AC topí, nebo venkovní teplota pod prahem, kde AC ztrácí výkon), AC nedostupná, nebo teplota pod nouzovým minimem.

### Koupelna

Kabel je jediný zdroj; předtápění přednostně v NT, cíl platí vždy.

### Proti kmitání

- AC: zapnutí/vypnutí nejvýše jednou za 10 min, změna cíle max o 1 °C za krok (stávající `thermal_controller.py` se použije pro regulaci AC uvnitř zóny).
- POER: pásmo necitlivosti ±0,3 °C.

### Léto

Beze změny proti dnešku: chlazení jen AC a jen v sezónách povolených `automation_rules.py`. POER termostaty v létě na nouzovém minimu.

## 7. Tarif

- Okna NT konfigurovatelná **v aplikaci**, pro každý typ dne (pracovní den / víkend) libovolný počet oken `HH:MM–HH:MM`.
- Výchozí orientační okna do doby, než uživatel zadá skutečné časy HDO.

## 8. Chyby a bezpečnost

- **POER jako poslední záchrana**: POER termostaty nikdy nedostanou cíl pod nouzové minimum. Když aplikace nebo server neběží, termostaty samy udrží minimum vlastním čidlem.
- Nedostupné zařízení → zóna použije jiný zdroj, UI zobrazí varování.
- Všechna vnitřní čidla zóny stará → zóna drží poslední bezpečné nastavení a neposílá nové příkazy.

## 9. Ukládání

- `data/zones.json` – struktura: zóny, zařízení, čidla, role. Mění se výjimečně, ručně; šablona `data/zones.json.example`.
- `data/control.json` – vše měněné z aplikace: režim, program, parametry automatiky, dovolená, tarif, přebití, prahy. Atomický zápis (stejný vzor jako `state.json`).
- Migrace: `state.json` `control_mode` HAND → Ručně, AUTO → Automatika. `schedule.json` (HAND scheduler zapni/vypni) nahrazuje Program; stávající záznamy se nemigrují.

## 10. Uživatelské rozhraní

- **Dashboard** – přepínač režimu; karta zóny: aktuální teplota + zdroj, cíl, aktivní zdroj tepla, důvod rozhodnutí, přebití se zbývajícím časem a tlačítkem Zrušit, varování o nedostupnosti.
- **Program** – týdenní mřížka Po–Ne, bloky, kopírování dnů.
- **Automatika** – cíle zón, noční útlum, přepínače slunce / předpověď / vysoušení.
- **Dovolená** – návrat, teploty nepřítomnosti, hodiny předtopení.
- **Nastavení** – okna NT, limit přebití, prahy krbu a okna, nouzové minimum, souřadnice.
- **Deník rozhodnutí** – posledních N rozhodnutí každé zóny s důvodem.

## 11. Testování

- Rozhodování zóny jako **čistá funkce** `(čidla, čas, tarif, nastavení, stav) → požadovaný stav zařízení + důvod`; scénářové unit testy (mráz + VT + AC nestačí, krb, okno, dovolená s předtopením, přebití, nouzové minimum…).
- Arbitr testovaný s falešnými zařízeními: sériovost, nahrazování, deduplikace, frekvence, opakování, označení nedostupnosti.
- **Zkušební provoz (dry-run)**: řídicí smyčka počítá a zapisuje do deníku, ale neposílá příkazy. Nasazení nejdřív v tomto režimu na několik dní.

## 12. Podprojekty a pořadí

Každý podprojekt má vlastní implementační plán a je samostatně nasaditelný.

1. **Ověření LG na živých datech** (průzkum) – reálný status/profil, ověření příkazů (měnící příkazy jen se souhlasem), zápis zjištění. Současně ověření obou POER termostatů (read-only `SYNC`/`QUERY`).
2. **Arbitr příkazů** – fronta, priority, deduplikace, frekvence; přesměrování všech cest na arbitra.
3. **POER multi-device** – klient pro více termostatů, jednotné rozhraní zařízení.
4. **Historie dat** (kap. 5.4) – sběr a ukládání; registr čidel se stářím hodnot a zdrojem `smart_plug` se přesouvá do podprojektu 5 (zóny).
5. **Zóny, režimy, program, dovolená, přebití** – `zones.json`, `control.json`, řídicí smyčka s dry-run, UI.
6. **Automatika a volba zdroje** – slunce, předpověď, vysoušení, tarif, logika fólie/AC.

## 13. Mimo rozsah (připraveno, neimplementuje se)

- Konkrétní hardware venkovních čidel a chytré zásuvky (jen rozhraní `http` / `smart_plug`).
- Automatická detekce přítomnosti (telefony apod.).
- Režim per zóna v UI (vnitřní model ho umožňuje).
- Automatické stahování časů HDO z webu distributora.
- Státní svátky jako víkend.
