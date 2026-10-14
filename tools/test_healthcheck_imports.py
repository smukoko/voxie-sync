#!/usr/bin/env python3
"""Every name the health check imports from the product still exists.

The checks import inside their own functions, so a name removed from core/
breaks only that check, and only when it runs: the English-only change
(2026-10-07) removed core.languages.MFA_MODELS and the daily check failed
with an ImportError for three days. This reads every `from core... import`
in tools/healthcheck.py and resolves it, with no network.

    python tools/test_healthcheck_imports.py
"""
import ast
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

tree = ast.parse(open(os.path.join(ROOT, "tools", "healthcheck.py")).read())
imports = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
           and (n.module or "").split(".")[0] == "core" for a in n.names]
missing = []
for module, name in imports:
    try:
        if not hasattr(importlib.import_module(module), name):
            missing.append(f"{module}.{name}")
    except ImportError as e:
        missing.append(f"{module} ({e})")

ok = bool(imports) and not missing
print(f"{'PASS' if ok else 'FAIL'}  the health check's {len(imports)} import(s) from core/ resolve"
      + (f": missing {', '.join(missing)}" if missing else ""))
sys.exit(0 if ok else 1)
