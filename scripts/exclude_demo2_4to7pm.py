"""One-off: excludes 16:00-19:00 Sri Lanka time (Asia/Colombo) from demo2's
trading sessions -- explicit user decision 2026-08-28: "only the demo 2,
demo 1 no need sri lankn 4:00 pm to 7:pm". demo1 is explicitly NOT touched
by this script.

demo2_m1 already had a separate {16:00-19:00} window in its session list
-- just removed outright, leaving {04:00-08:00}, {12:00-16:00},
{19:00-22:00}.

demo2_m3 had one big wrap-around window {04:00-01:29} covering the whole
day (minus a small 01:29-04:00 dead zone) -- split into two to carve out
the same gap: {04:00-16:00} and {19:00-01:29} (still wraps past midnight,
same as the original single window did -- bot/sessions.py's
is_within_session already supports this).

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

# ---- demo2_m3 ----
p3 = Path("config/settings.demo2_m3.yaml")
with open(p3, "r", encoding="utf-8-sig") as f:
    raw3 = yaml.safe_load(f)

old_m3_sessions = raw3["sessions"]["dual_cross_confirmed_swap"]
assert old_m3_sessions == [{"start": "04:00", "end": "01:29"}], (
    f"demo2_m3's sessions weren't the expected single {{04:00-01:29}} window "
    f"(got {old_m3_sessions}) -- stopping rather than guessing how to split it."
)
new_m3_sessions = [{"start": "04:00", "end": "16:00"}, {"start": "19:00", "end": "01:29"}]
raw3["sessions"]["dual_cross_confirmed_swap"] = new_m3_sessions

with open(p3, "w", encoding="utf-8") as f:
    yaml.dump(raw3, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p3}: split {{04:00-01:29}} into {new_m3_sessions}")

# ---- Verify via real load_config() ----
m1_config = load_config("demo2_m1")
m3_config = load_config("demo2_m3")
m1_windows = [(w.start, w.end) for w in m1_config.sessions["dual_cross_confirmed_swap"]]
m3_windows = [(w.start, w.end) for w in m3_config.sessions["dual_cross_confirmed_swap"]]
print(f"\nload_config('demo2_m1') sessions: {m1_windows}")
print(f"load_config('demo2_m3') sessions: {m3_windows}")
assert ("16:00", "19:00") not in m1_windows
assert ("04:00", "16:00") in m3_windows and ("19:00", "01:29") in m3_windows
print("Confirmed: no 16:00-19:00 window remains active on either demo2 leg.")

# ---- Sanity check: demo1_m1/demo1_m3 untouched (separate config files, but verify explicitly anyway) ----
d1m1 = load_config("demo1_m1")
d1m3 = load_config("demo1_m3")
print(f"\ndemo1_m1 sessions (should be unaffected): {[(w.start, w.end) for w in d1m1.sessions[d1m1.strategy_variant]]}")
print(f"demo1_m3 sessions (should be unaffected): {[(w.start, w.end) for w in d1m3.sessions[d1m3.strategy_variant]]}")
