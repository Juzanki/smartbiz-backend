from __future__ import annotations
import importlib, pkgutil, traceback
from sqlalchemy import inspect
from sqlalchemy.orm import configure_mappers
from backend.db import engine

def import_all_models():
    pkg = importlib.import_module("backend.models")
    for _, modname, ispkg in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        if ispkg:
            continue
        try:
            importlib.import_module(modname)
        except Exception as e:
            print(f"[IMPORT-ERROR] {modname}: {e}")
            traceback.print_exc()

def print_foreign_keys():
    insp = inspect(engine)
    print("\n=== FOREIGN KEYS MAP ===")
    for t in sorted(insp.get_table_names()):
        fks = insp.get_foreign_keys(t)
        if not fks:
            continue
        print(f"\n[{t}]")
        for fk in fks:
            cols = ", ".join(fk.get("constrained_columns", []))
            ref_tbl = fk.get("referred_table")
            ref_cols = ", ".join(fk.get("referred_columns", []))
            name = fk.get("name", "?")
            print(f" - {name}: ({cols}) -> {ref_tbl}({ref_cols})")

def suggest_user_fk_disambiguation():
    insp = inspect(engine)
    print("\n=== SUGGEST: Tables with multiple FKs to users ===")
    for t in sorted(insp.get_table_names()):
        user_fk_cols = []
        for fk in insp.get_foreign_keys(t) or []:
            if fk.get("referred_table") == "users":
                user_fk_cols.extend(fk.get("constrained_columns", []))
        uniq = sorted(set(user_fk_cols))
        if len(uniq) >= 2:
            print(f"- {t}: multiple FKs to users -> {uniq}")
            print("  Use foreign_keys= and primaryjoin= on relationships targeting this table.")

def try_configure_mappers():
    print("\n=== CONFIGURE MAPPERS ===")
    try:
        configure_mappers()
        print("OK: All mappers configured successfully.")
    except Exception as e:
        print("ERROR during configure_mappers:\n", e)
        traceback.print_exc()

def main():
    import_all_models()
    print_foreign_keys()
    suggest_user_fk_disambiguation()
    try_configure_mappers()

if __name__ == "__main__":
    main()
