"""One-off: retires demo1_ce, matching the same pattern used to retire the
original demo1 account (see project_reliability_incident.md) — renames its
config file so main.py fails fast (FileNotFoundError) instead of silently
starting, if anything ever tries to launch it again (a Scheduled Task
that's merely disabled, or the gateway's direct-launch endpoint, could
otherwise resurrect it). Disabling the Scheduled Tasks is done separately
in PowerShell (this script only touches the config file).
"""
from pathlib import Path

src = Path("config/settings.demo1_ce.yaml")
dst = Path("config/settings.demo1_ce.yaml.retired-superseded-demo1_m1_and_demo1_m3_switched")

if src.exists():
    src.rename(dst)
    print(f"Renamed {src} -> {dst}")
else:
    print(f"{src} does not exist (already retired?)")

# Verify: load_config('demo1_ce') should now fail loudly.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

try:
    load_config("demo1_ce")
    print("WARNING: load_config('demo1_ce') still succeeded — retirement did not take effect!")
except FileNotFoundError as e:
    print(f"Confirmed: load_config('demo1_ce') now fails as expected: {e}")
