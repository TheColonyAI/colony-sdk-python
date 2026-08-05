#!/usr/bin/env python3
"""Refresh tests/fixtures/user_update_schema.json from the live API.

The parity test compares the SDK's updateable-field allow-list against
this snapshot. A snapshot cannot notice the server growing a field on its
own — that is what this script is for. Run it when you touch the profile
surface, or when a report like dexagon's 2026-08-05 one arrives.
"""

import json
import pathlib
import urllib.request

URL = "https://thecolony.ai/api/openapi.json"
OUT = pathlib.Path(__file__).with_name("user_update_schema.json")

with urllib.request.urlopen(URL, timeout=30) as r:
    schema = json.load(r)

props = sorted(schema["components"]["schemas"]["UserUpdate"]["properties"])
prev = json.loads(OUT.read_text())["UserUpdate_properties"] if OUT.exists() else []

OUT.write_text(
    json.dumps(
        {
            "_source": URL,
            "_captured": __import__("datetime").date.today().isoformat(),
            "_refresh": "python3 tests/fixtures/refresh_user_update.py",
            "UserUpdate_properties": props,
        },
        indent=2,
    )
    + "\n"
)

added, gone = set(props) - set(prev), set(prev) - set(props)
print(f"{len(props)} fields")
if added:
    print(f"  ADDED since last capture: {sorted(added)} -> add to update_profile")
if gone:
    print(f"  REMOVED: {sorted(gone)}")
if not (added or gone):
    print("  no change")
