"""One-off: excludes 16:00-19:00 Sri Lanka time (Asia/Colombo) from
demo2_m1's trading sessions -- explicit user decision 2026-08-28:
"only the demo 2, demo 1 no need sri lankn 4:00 pm to 7:pm", then
clarified same day to demo2_m1 ONLY, not demo2_m3 ("no need m3"). Neither
demo1_m1/demo1_m3 nor demo2_m3 are touched by this script.

demo2_m1 already had a separate {16:00-19:00} window in its session list
-- just removed outright, leaving {04:00-08:00}, {12:00-16:00},
{19:00-22:00}.

session windows are interpreted in Asia/Colombo time (confirmed via
bot/sessions.py's COLOMBO = ZoneInfo("Asia/Colombo") / is_within_session's
now_utc.astimezone(COLOMBO)), so no additional timezone conversion is
needed here -- the requested "Sri Lanka time" window maps directly onto
these config strings as-is.
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

# ---- demo2_m1 ----
p1 = Path("config/settings.demo2_m1.yaml")
with open(p1, "r", encoding="utf-8-sig") as f:
    raw1 = yaml.safe_load(f)

old_m1_sessions = raw1["sessions"]["dual_cross_confirmed_swap"]
new_m1_sessions = [w for w in old_m1_sessions if not (w["start"] == "16:00" and w["end"] == "19:00")]
assert len(new_m1_sessions) == len(old_m1_sessions) - 1, (
    f"Expected to remove exactly one {{16:00-19:00}} window, but went from "
    f"{len(old_m1_sessions)} to {len(new_m1_sessions)} windows -- config shape may have changed."
)
raw1["sessions"]["dual_cross_confirmed_swap"] = new_m1_sessions

with open(p1, "w", encoding="utf-8") as f:
    yaml.dump(raw1, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p1}: removed {{16:00-19:00}} window. Remaining: {new_m1_sessions}")

# ---- Verify via real load_config() ----
m1_config = load_config("demo2_m1")
m1_windows = [(w.start, w.end) for w in m1_config.sessions["dual_cross_confirmed_swap"]]
print(f"\nload_config('demo2_m1') sessions: {m1_windows}")
assert ("16:00", "19:00") not in m1_windows
print("Confirmed: no 16:00-19:00 window remains active on demo2_m1.")

# ---- Sanity check: demo2_m3, demo1_m1, demo1_m3 all untouched ----
m3_config = load_config("demo2_m3")
m3_windows = [(w.start, w.end) for w in m3_config.sessions["dual_cross_confirmed_swap"]]
assert m3_windows == [("04:00", "01:29")], (
    f"demo2_m3's sessions changed unexpectedly (got {m3_windows}) -- this script should never touch demo2_m3!"
)
print(f"Confirmed: demo2_m3 unaffected, still {m3_windows}")

d1m1 = load_config("demo1_m1")
d1m3 = load_config("demo1_m3")
print(f"demo1_m1 sessions (should be unaffected): {[(w.start, w.end) for w in d1m1.sessions[d1m1.strategy_variant]]}")
print(f"demo1_m3 sessions (should be unaffected): {[(w.start, w.end) for w in d1m3.sessions[d1m3.strategy_variant]]}")
