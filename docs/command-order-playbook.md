# LG ThinQ Command Order Playbook

Tento dokument popisuje bezpecne poradi prikazu pro klimatizaci, aby se minimalizovaly kolize,
odmitnute prikazy a riziko prekroceni API limitu.

Platnost:
- Projekt: LG_projekt
- API model: ThinQ OpenAPI (PAT)
- SDK: thinqconnect 1.0.12

## 1. Zakladni principy

1. MQTT je primarni zdroj stavu.
2. HTTP se pouziva pro control prikazy a fallback refresh.
3. Prikazy se musi serializovat (zadny paralelni control na stejny device).
4. Po nekterych krocich je nutna mala pauza (device-side sync).
5. Retry/backoff pouzivat jen pro retryovatelne chyby.

## 2. Bezpecne poradi kroku

Doporucene poradi pri zmene nastaveni klimatizace:

1. Nacist posledni znamy stav (idealne z MQTT cache, fallback HTTP).
2. Pokud je zarizeni vypnute a chceme menit provozni parametry, nejdriv POWER_ON.
3. Pockat cca 2.5 s po POWER_ON.
4. Pokud je potreba, zmenit mode.
5. Pockat cca 2.0 s po zmene mode.
6. Nastavit target temperature (ne v mode FAN).
7. Nastavit wind strength / direction.
8. Nastavit power save / timery.
9. Provest jeden slouceny refresh stavu (pri MQTT online pouze fallback refresh).

## 3. Preconditions podle typu prikazu

- power_on:
  - Pokud uz je POWER_ON, prikaz preskocit.
- power_off:
  - Pokud uz je POWER_OFF, prikaz preskocit.
- change_mode:
  - Vyzaduje zapnute zarizeni.
  - Pokud je mode uz cilovy, prikaz preskocit.
- set_temperature:
  - Vyzaduje zapnute zarizeni.
  - V mode FAN se teplota nemenit.
- set_wind_strength:
  - Vyzaduje zapnute zarizeni.
  - Pokud je hodnota stejna, prikaz preskocit.
- set_wind_direction:
  - Vyzaduje zapnute zarizeni.
  - Pokud je hodnota stejna, prikaz preskocit.
- set_rotate_updown:
  - Vyzaduje zapnute zarizeni.
  - Odesila POUZE klic `rotateUpDown` v payload `windDirection` – nesmi byt kombinovano s `rotateLeftRight` v jednom pozadavku (device-side konflikt).
  - Pokud je hodnota stejna jako aktualni stav, prikaz preskocit.
- set_rotate_leftright:
  - Vyzaduje zapnute zarizeni.
  - Odesila POUZE klic `rotateLeftRight` v payload `windDirection` – viz poznamka u `set_rotate_updown`.
  - Pokud je hodnota stejna jako aktualni stav, prikaz preskocit.
- set_power_save:
  - Vyzaduje zapnute zarizeni.
  - Pokud je hodnota stejna, prikaz preskocit.
- set_sleep_timer:
  - Vyzaduje zapnute zarizeni.
- cancel_all_timers:
  - Muze bezet i samostatne.

## 4. Retry a limit policy

Retry/backoff je aktivni pro:
- ThinQ error code 1306 (EXCEEDED_API_CALLS)
- ThinQ error code 2210 (RETRY_REQUEST)
- HTTP 429 a 5xx
- transient sitove chyby (aiohttp ClientError, timeout)

Doporuceny model:
- exponential backoff + jitter
- max 3-4 pokusy dle typu operace
- control prikazy opakovat konzervativne (max 3 pokusy)

## 5. MQTT relace a zivotni cyklus

- Relace zustava aktivni po celou dobu behu aplikace.
- Pri preruseni spojeni se SDK pokusi o reconnect.
- Pri zavreni aplikace se dela korektni async_disconnect.

Dulezite:
- Pokud aplikaci vypnes, MQTT relace skonci.
- Po restartu aplikace probiha znovu inicializace klienta a certifikatu.

## 6. Kde je to implementovano v kodu

- Retry/backoff a API ochrana:
  - src/server_api.py
- Command policy (preconditions + plan kroku):
  - src/command_policy.py
- Sdilena logika provedeni prikazu (CLI i web):
  - src/command_executor.py
- Web API endpoint pro prikazy:
  - src/web/routes/control.py
- CLI prikazy (parsovani + execute):
  - src/main.py

## 7. Operacni checklist (pro budoucnost)

Pri navratu k projektu po delsi dobe:

1. Overit verzi Python a venv.
2. Overit verzi thinqconnect.
3. Overit, ze config.json obsahuje platny PAT a client_id.
4. Overit, ze devices.json ma spravne deviceId.
5. Spustit CLI --status pro rychly smoke test.
6. Spustit web (--mode web), overit /health endpoint a MQTT stav v logu.
7. Otestovat jeden bezpecny prikaz (power_on nebo power_off).
9. Zkontrolovat logy na chybove kody 1306, 2210, 2304.

## 8. Dalsi kroky (neimplementovano)

1. MQTT watchdog s automatickym fallback pollingem pri delsim vypadku.
2. Persistovana telemetrie API (latence, pocty volani, error kody).
3. Scheduler executor – background runner pro HAND plany (aktualne je schedule.json jen uloziste).
4. Integracni testy proti mock API modelu.
