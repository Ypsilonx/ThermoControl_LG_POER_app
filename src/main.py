# -*- coding: utf-8 -*-
"""
Hlavní vstupní bod aplikace ThermoControl-LG-POER_app.
Režimy: web (výchozí, FastAPI server) a cli (jednorázové příkazy a stav).
"""
import sys
import argparse
import asyncio
from pathlib import Path
from typing import Any

from env_config import load_local_env

# Zajistíme, že Python najde naše moduly
sys.path.insert(0, str(Path(__file__).parent))
load_local_env()
from server_api import get_ac_device_id, get_device_id_by_alias, list_devices
from device_jobs import lg_command_job

def main():
    """Hlavní funkce aplikace"""
    parser = argparse.ArgumentParser(description="ThermoControl-LG-POER_app")
    parser.add_argument("--mode", choices=["cli", "web"], default="web",
                       help="Režim spuštění: web (výchozí) nebo cli")
    parser.add_argument("--list-devices", action="store_true",
                       help="Vypíše dostupná zařízení z devices.json (CLI)")
    parser.add_argument("--device-id", type=str,
                       help="ID zařízení (pro CLI režim)")
    parser.add_argument("--device-alias", type=str,
                       help="Alias zařízení z devices.json (pro CLI režim)")
    parser.add_argument("--command", type=str,
                       help="Příkaz pro zařízení (pro CLI režim)")
    parser.add_argument("--status", action="store_true",
                       help="Zobrazit stav zařízení (CLI)")
    
    args = parser.parse_args()
    
    if args.mode == "web":
        run_web()
    elif args.mode == "cli":
        # CLI režim
        print("ThermoControl-LG-POER_app - CLI režim")
        
        if args.list_devices:
            cli_list_devices()
        elif args.status:
            # Zobrazení stavu zařízení
            asyncio.run(cli_show_status(args.device_id, args.device_alias))
        elif args.command:
            # Provedení příkazu
            asyncio.run(cli_execute_command(args.device_id, args.command, args.device_alias))
        else:
            print("Pro CLI režim zadejte --status, --command nebo --list-devices")
            parser.print_help()

def run_web():
    """
    Spustí webový server (FastAPI + uvicorn).

    Konfigurace (host, port, úroveň logů, reload, důvěryhodné proxy IP) se
    čte z proměnných prostředí přes ``web.settings.get_settings``. Díky
    ``proxy_headers`` a ``forwarded_allow_ips`` server správně rozpozná
    skutečnou IP klienta i za reverzní proxy (Cloudflare Tunnel, nginx).
    """
    try:
        import uvicorn
    except ImportError:
        print("Chyba: uvicorn není nainstalován. Spusťte: pip install uvicorn[standard]")
        sys.exit(1)

    import logging
    from web.settings import get_settings

    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s – %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"Spouštím webový server – http://{settings.host}:{settings.port}")
    if settings.docs_enabled:
        print(f"Swagger API docs: http://localhost:{settings.port}/docs")
    # Předáváme string 'web.app:app' – src/ je v sys.path (přidáno výše v main.py),
    # takže uvicorn najde modul web/app.py správně.
    # proxy_headers + forwarded_allow_ips: za reverzní proxy se použije
    # skutečná IP klienta z X-Forwarded-For (nutné pro logy i rate limiting).
    uvicorn.run(
        "web.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


def resolve_device_id(device_id: str | None = None, device_alias: str | None = None) -> str:
    """
    Vyhodnotí cílové zařízení z argumentů CLI.

    Args:
        device_id: Explicitní Device ID
        device_alias: Alias zařízení z devices.json

    Returns:
        str: Výsledné Device ID

    Raises:
        ValueError: Pokud je zadán neplatný alias nebo kombinace argumentů
    """
    if device_id:
        return device_id
    if device_alias:
        return get_device_id_by_alias(device_alias)
    return get_ac_device_id()


def cli_list_devices():
    """Vypíše seznam zařízení načtených z devices.json."""
    try:
        devices = list_devices()
        if not devices:
            print("V devices.json nebyla nalezena žádná zařízení.")
            return

        print("\nDostupná zařízení:")
        print("-" * 72)
        print(f"{'Alias':<20} {'Typ':<28} {'ID (zkráceně)':<20}")
        print("-" * 72)

        for device in devices:
            alias = device.get("alias") or "(bez aliasu)"
            dev_type = device.get("device_type") or "(neznámý typ)"
            dev_id = device.get("device_id") or "(bez ID)"
            short_id = f"{dev_id[:8]}..." if len(dev_id) > 8 else dev_id
            print(f"{alias:<20} {dev_type:<28} {short_id:<20}")

    except Exception as e:
        print(f"Chyba při načítání zařízení: {e}")


async def cli_show_status(device_id=None, device_alias=None):
    """CLI funkce pro zobrazení stavu zařízení"""
    try:
        from server_api import ThinQAPI
        
        api = ThinQAPI()
        await api.initialize()
        
        # Použití zvoleného zařízení (ID, alias, nebo výchozí klimatizace)
        device_id = resolve_device_id(device_id, device_alias)
            
        status = await api.get_device_status(device_id)
        
        # Podle device_profile.json: kombinace runState a operation
        run_state = status.get("runState", {}).get("currentState", "N/A")
        power_operation = status.get("operation", {}).get("airConOperationMode", "N/A")
        
        print(f"\n=== Stav klimatizace (ID: {device_id[:8]}...) ===")
        print(f"Napájení: {power_operation} (Běh: {run_state})")
        print(f"Režim: {status.get('airConJobMode', {}).get('currentJobMode', 'N/A')}")
        print(f"Aktuální teplota: {status.get('temperature', {}).get('currentTemperature', 'N/A')}°C")
        print(f"Cílová teplota: {status.get('temperature', {}).get('targetTemperature', 'N/A')}°C")
        print(f"Síla větru: {status.get('airFlow', {}).get('windStrength', 'N/A')}")
        print(f"Úspora energie: {status.get('powerSave', {}).get('powerSaveEnabled', False)}")
        
        await api.close()
        
    except Exception as e:
        print(f"Chyba při získávání stavu: {e}")

async def cli_execute_command(device_id, command, device_alias=None):
    """CLI funkce pro provedení příkazu"""
    try:
        from server_api import ThinQAPI
        
        api = ThinQAPI()
        await api.initialize()
        
        device_id = resolve_device_id(device_id, device_alias)

        internal_command, internal_args = parse_cli_command(command)

        # CLI je samostatný proces – arbitr web serveru ho koordinovat nemůže,
        # proto jen stejná úloha (čerstvý stav + command pipeline) bez fronty.
        outcome = await lg_command_job(api, device_id, internal_command, internal_args)()
        if not outcome.sent:
            print(f"Příkaz přeskočen: {outcome.skip_reason}")

        for step_result in outcome.steps:
            print(f"Příkaz '{step_result['step']}' úspěšně odeslán: {step_result['result']}")
        
        await api.close()
        
    except Exception as e:
        print(f"Chyba při provádění příkazu: {e}")


def parse_cli_command(command: str) -> tuple[str, tuple[Any, ...]]:
    """
    Přeloží textový CLI příkaz na interní příkaz a argumenty.

    Args:
        command: Hodnota z argumentu --command

    Returns:
        tuple[str, tuple[Any, ...]]: Interní příkaz a jeho argumenty

    Raises:
        ValueError: Pokud příkaz není podporován
    """
    command_lower = command.lower()

    if command_lower == "power_on":
        return "power_on", ()
    if command_lower == "power_off":
        return "power_off", ()
    if command_lower.startswith("mode_"):
        mode = command.replace("mode_", "").upper()
        return "change_mode", (mode,)
    if command_lower.startswith("temp_"):
        temp = float(command.replace("temp_", ""))
        return "set_temperature", (temp,)

    raise ValueError(
        "Neznámý příkaz. Dostupné: power_on, power_off, mode_cool, mode_heat, "
        "mode_fan, mode_auto, temp_22, atd."
    )


if __name__ == "__main__":
    main()
