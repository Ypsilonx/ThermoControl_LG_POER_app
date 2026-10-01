# -*- coding: utf-8 -*-
"""
Setup script pro inicializaci ThermoControl-LG-POER_app projektu.
Zkopíruje šablony konfiguračních souborů a provede základní nastavení.
"""
import shutil
from pathlib import Path

def setup_project():
    """Inicializace projektu - vytvoření config souborů z šablon"""
    data_dir = Path(__file__).parent / "data"
    
    configs = [
        ("config.json.example", "config.json"),
        ("devices.json.example", "devices.json"),
        ("schedule.json.example", "schedule.json"),
        ("automation_rules.json.example", "automation_rules.json"),
    ]
    
    print("🚀 Inicializace ThermoControl-LG-POER_app projektu...\n")
    
    for example, target in configs:
        example_path = data_dir / example
        target_path = data_dir / target
        
        if not example_path.exists():
            print(f"⚠️  Šablona {example} nebyla nalezena!")
            continue
            
        if target_path.exists():
            print(f"ℹ️  {target} už existuje - přeskakuji")
        else:
            shutil.copy(example_path, target_path)
            print(f"✅ Vytvořen {target}")
    
    print("\n📝 DALŠÍ KROKY:")
    print("1. Upravte data/config.json s vašimi LG ThinQ API údaji")
    print("2. Získejte API přístup na: https://developer.lgaccount.com/")
    print("3. Upravte data/devices.json s ID vašich zařízení")
    print("4. Spusťte aplikaci: python src/main.py")
    print("\n✨ Hotovo! Projekt je připraven k použití.\n")

if __name__ == "__main__":
    setup_project()
