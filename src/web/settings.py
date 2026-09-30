# -*- coding: utf-8 -*-
"""
Centrální konfigurace webového serveru načítaná z proměnných prostředí.

Veškeré produkční chování (host/port, úroveň logů, povolení Swagger UI,
režim autentizace, nastavení reverzní proxy a rate limiting) se řídí
proměnnými prostředí s předponou ``LG_``. Díky tomu lze stejný obraz
nasadit lokálně i v produkci bez úpravy kódu – stačí změnit ``.env``.

Příklad viz soubor ``.env.example`` v kořeni projektu.
"""

import os
from functools import lru_cache

from env_config import load_local_env

load_local_env()


def _env_bool(name: str, default: bool) -> bool:
    """
    Přečte boolean hodnotu z proměnné prostředí.

    Akceptuje ``1/true/yes/on`` (case-insensitive) jako True,
    ostatní neprázdné hodnoty jako False.

    Args:
        name:    Název proměnné prostředí.
        default: Výchozí hodnota, pokud proměnná není nastavena.

    Returns:
        bool: Vyhodnocená boolean hodnota.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """
    Přečte celočíselnou hodnotu z proměnné prostředí.

    Args:
        name:    Název proměnné prostředí.
        default: Výchozí hodnota při chybějící nebo neplatné hodnotě.

    Returns:
        int: Vyhodnocené číslo (při chybě se vrací default).
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_power_map(raw: str) -> dict[str, float]:
    """
    Přečte příkony topení ve tvaru ``id:W,id:W`` (např. ``fee89300f2a5:3500``).

    Args:
        raw: Hodnota proměnné prostředí

    Returns:
        dict[str, float]: Příkon v kW podle ID zařízení; neplatné položky se vynechají
    """
    result: dict[str, float] = {}
    for item in raw.split(","):
        device, _, watts = item.strip().partition(":")
        try:
            result[device.strip()] = float(watts) / 1000
        except ValueError:
            continue
    return {device: kw for device, kw in result.items() if device}


class Settings:
    """
    Snímek konfigurace serveru sestavený z proměnných prostředí.

    Atributy:
        host:                 Adresa, na které uvicorn naslouchá.
        port:                 Port serveru.
        log_level:            Úroveň logování uvicornu (info/warning/...).
        reload:               Auto-reload kódu (pouze pro vývoj!).
        docs_enabled:         Povolení Swagger UI (/docs) a ReDoc (/redoc).
        forwarded_allow_ips:  IP reverzní proxy, jimž se věří X-Forwarded-* hlavičky.
        auth_mode:            "cloudflare" (ověřuje Cloudflare Access JWT) nebo "none".
        cf_team_domain:       URL Cloudflare Access týmu (bez koncového lomítka).
        cf_aud:               Application Audience (AUD) tag z Cloudflare Access.
        rate_limit_enabled:   Zapnutí rate limitu na příkazové endpointy.
        rate_limit_max:       Maximální počet příkazů za okno.
        rate_limit_window_s:  Délka okna v sekundách.
        history_enabled:      Sběr historie dat do data/history.db.
        history_poll_s:       Interval odečtu POER termostatů pro historii (s).
        history_retention_days: Jak dlouho se historie uchovává (dny).
        poer_power_kw:        Příkon topení podle ID termostatu (odhad kWh v exportu).
    """

    def __init__(self) -> None:
        # --- Síť a běh serveru ---
        self.host: str = os.getenv("LG_HOST", "0.0.0.0")
        self.port: int = _env_int("LG_PORT", 8000)
        self.log_level: str = os.getenv("LG_LOG_LEVEL", "info").lower()
        self.reload: bool = _env_bool("LG_RELOAD", False)

        # --- Swagger / ReDoc ---
        # Ve výchozím stavu vypnuto – v produkci se nemá vystavovat API mapa.
        self.docs_enabled: bool = _env_bool("LG_DOCS_ENABLED", False)

        # --- Reverzní proxy (Cloudflare Tunnel, nginx, ...) ---
        # uvicorn bude důvěřovat X-Forwarded-* hlavičkám jen od těchto IP.
        # "*" znamená důvěřovat všem (vhodné jen za uzavřeným tunelem).
        self.forwarded_allow_ips: str = os.getenv("LG_FORWARDED_ALLOW_IPS", "127.0.0.1")

        # --- Autentizace ---
        self.auth_mode: str = os.getenv("LG_AUTH_MODE", "none").lower()
        self.cf_team_domain: str = os.getenv("LG_CF_TEAM_DOMAIN", "").rstrip("/")
        self.cf_aud: str = os.getenv("LG_CF_AUD", "")

        # --- Rate limiting (ochrana příkazových endpointů) ---
        self.rate_limit_enabled: bool = _env_bool("LG_RATE_LIMIT_ENABLED", True)
        self.rate_limit_max: int = _env_int("LG_RATE_LIMIT_MAX", 30)
        self.rate_limit_window_s: int = _env_int("LG_RATE_LIMIT_WINDOW_S", 60)

        # --- Historie dat ---
        self.history_enabled: bool = _env_bool("LG_HISTORY_ENABLED", True)
        self.history_poll_s: int = max(60, _env_int("LG_HISTORY_POLL_S", 300))
        self.history_retention_days: int = max(1, _env_int("LG_HISTORY_RETENTION_DAYS", 90))
        self.poer_power_kw: dict[str, float] = _parse_power_map(
            os.getenv("LG_POER_POWER_W", "")
        )

    @property
    def auth_is_cloudflare(self) -> bool:
        """True, pokud je aktivní ověřování přes Cloudflare Access."""
        return self.auth_mode == "cloudflare"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Vrátí singleton instanci :class:`Settings`.

    Konfigurace se čte jednou při prvním volání a poté se cachuje –
    proměnné prostředí se za běhu serveru nemění.

    Returns:
        Settings: Sdílená instance konfigurace.
    """
    return Settings()
