"""The generated leg config must be loadable YAML with the right values.

    python3 tests/test_new_leg_config.py      (needs pyyaml)

On 2026-09-09 create_m5_leg.py wrote config/settings.demo1_m5.yaml and
the very next command died with

    yaml.parser.ParserError: while parsing a block mapping
    expected <block end>, but found '-'

It had been editing the config as TEXT, substituting keys line by line
and appending any key it did not find to the end of the file -- which
puts a nested key like sibling_magic_numbers at top level and breaks the
document. The file was already written before anyone knew.

Two lessons, both encoded here: build the document through the parser so
an invalid file cannot be emitted, and assert on a REAL committed config
rather than a hand-made fixture, so the test sees the actual shape --
nested execution block, list-of-dicts sizing ladder, per-variant
sessions.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from create_m5_leg import ARM_BEFORE, build_document

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> None:
    print("generated leg config")
    # A real committed config, so the test sees the real shape.
    src = yaml.safe_load((ROOT / "config" / "settings.demo2_m3.yaml").read_text(encoding="utf-8"))

    stop, tp = 10.0, 10.0
    scale = 0.667
    doc = build_document(src, timeframe="M5", stop=stop, take_profit=tp,
                         breakeven=round(tp - ARM_BEFORE, 2), trail=None, lock_below=None,
                         magic=910005, siblings=[910003, 910001], scale=scale)

    # 1. It must survive a full round trip -- this is the bug that shipped.
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    try:
        back = yaml.safe_load(text)
        parsed = True
    except yaml.YAMLError as exc:
        back, parsed = None, False
        print(f"        {exc}")
    check("dumps and re-parses as valid YAML", parsed)

    # 2. Values.
    check("timeframe is M5", back["timeframe"] == "M5")
    check("stop and take-profit as given",
          back["stop_loss_usd"] == 10.0 and back["take_profit_usd"] == 10.0)
    check("breakeven is derived as take_profit - arm_before",
          back["breakeven_trigger_usd"] == round(tp - ARM_BEFORE, 2))
    check("breakeven never sits above the arm point",
          back["breakeven_trigger_usd"] <= back["take_profit_usd"] - back["tp_runner_arm_before_usd"])
    check("arm_before is not scaled with the candle", back["tp_runner_arm_before_usd"] == 1.00)
    check("runner is off when no trail was fitted", back["tp_runner_trail_usd"] is None)
    check("swap_immediate is on", back["swap_immediate"] is True)

    # 3. Nesting survived -- this is exactly what the text version destroyed.
    check("magic_number stayed INSIDE execution", back["execution"]["magic_number"] == 910005)
    check("sibling_magic_numbers stayed INSIDE execution",
          back["execution"]["sibling_magic_numbers"] == [910003, 910001])
    check("no stray top-level magic_number", "magic_number" not in back)
    check("no stray top-level sibling_magic_numbers", "sibling_magic_numbers" not in back)

    # 4. The sizing ladder is rescaled, still a list of dicts, floor respected.
    ladder = back["position_sizing"]
    src_ladder = src["position_sizing"]
    check("sizing ladder is still a list of mappings",
          isinstance(ladder, list) and all(isinstance(t, dict) for t in ladder))
    check("ladder has the same number of tiers", len(ladder) == len(src_ladder))
    check("top tier scaled down (0.12 -> 0.08)", ladder[-1]["lots"] == 0.08)
    check("no tier below the 0.01 broker minimum", all(t["lots"] >= 0.01 for t in ladder))
    check("max_balance tiers untouched",
          [t["max_balance"] for t in ladder] == [t["max_balance"] for t in src_ladder])

    # 5. The source must not be mutated -- it is another live account's config.
    check("the source document was not modified",
          src["timeframe"] == "M3" and src["execution"]["magic_number"] == 920003)

    # 6. Sessions carry over intact, keyed by the strategy variant.
    check("sessions survived for the active variant",
          back["strategy_variant"] in back["sessions"]
          and back["sessions"] == src["sessions"])

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
