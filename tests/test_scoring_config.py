"""Validate config/scoring.yaml for internal consistency.

These are cheap checks that catch the class of mistake that otherwise only shows up
as a quietly wrong leaderboard: weights that do not sum to 1, a dimension referenced
by the scorer but absent from the config, a band table that is not monotonically
ordered, or a penalty set whose total can never reach the integrity gate.

Run: python tests/test_scoring_config.py
"""
from __future__ import annotations

import pathlib
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("SKIP: PyYAML not installed (pip install pyyaml)")
    sys.exit(0)

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config" / "scoring.yaml"

DIMENSIONS = [
    "early_entry",
    "profitability",
    "repeatability",
    "risk",
    "token_selection",
    "exit_effectiveness",
    "integrity",
]

EXPECTED_SUBSCORE_DIMS = set(DIMENSIONS) | {"confidence"}

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text())
    print(f"loaded {CONFIG.name} version {cfg.get('version')}\n")

    check("version present", isinstance(cfg.get("version"), str))
    check("frozen flag is a boolean", isinstance(cfg.get("frozen"), bool))

    weights = cfg["weights"]
    total = sum(weights.values())
    check(
        "dimension weights sum to 1.0",
        abs(total - 1.0) < 1e-9,
        f"sum={total!r}",
    )
    check(
        "every weighted dimension is a known dimension",
        set(weights) == set(DIMENSIONS),
        f"unexpected={set(weights) ^ set(DIMENSIONS)}",
    )
    check("no negative weights", all(w > 0 for w in weights.values()))

    for dim in DIMENSIONS:
        check(f"weights.{dim} has a matching config block", dim in cfg)

    check("confidence is NOT weighted into the overall score", "confidence" not in weights)

    # Sub-dimension weights must each sum to 1 where the doc declares a composition.
    for dim, subkey in [
        ("early_entry", None),
        ("profitability", None),
        ("repeatability", None),
        ("risk", None),
        ("token_selection", None),
        ("exit_effectiveness", None),
    ]:
        block = cfg[dim]
        sub = {k: v for k, v in block.items() if k.startswith("weight_")}
        if sub:
            s = sum(sub.values())
            check(
                f"{dim} sub-weights sum to 1.0",
                abs(s - 1.0) < 1e-9,
                f"sum={s!r} from {sub}",
            )

    # Bands must be strictly descending so that band lookup is unambiguous.
    bands = cfg["bands"]
    order = ["track", "watch", "investigate", "deprioritize"]
    check("band keys are exactly the expected set", set(bands) == set(order), f"got {set(bands)}")
    values = [bands[k] for k in order]
    check(
        "bands are strictly descending",
        all(a > b for a, b in zip(values, values[1:])),
        f"got {values}",
    )
    check("deprioritize is the floor band", bands["deprioritize"] == 0)

    # Gates
    gates = cfg["gates"]
    for key in ("integrity_min", "confidence_min", "min_closed_positions"):
        check(f"gates.{key} present and numeric", isinstance(gates.get(key), (int, float)))
    check(
        "integrity gate is reachable: 100 - max stack of penalties can fall below it",
        sum(cfg["integrity"]["penalties"].values()) > gates["integrity_min"],
        "no combination of penalties could ever trip the gate",
    )
    check(
        "confidence gate is below the 'track' band requirement",
        gates["confidence_min"] < bands["track"],
    )

    # Shrinkage
    shr = cfg["shrinkage"]
    check("shrinkage prior is on the 0-100 scale", 0 <= shr["prior"] <= 100)
    check(
        "every shrunk dimension has a k value",
        set(shr["k"]) >= set(DIMENSIONS) - {"integrity"},
        f"missing={set(DIMENSIONS) - {'integrity'} - set(shr['k'])}",
    )
    check("shrinkage k values are non-negative", all(v >= 0 for v in shr["k"].values()))

    # Dimension-specific sanity
    ee = cfg["early_entry"]
    check(
        "early_entry mcap_reference < mcap_ceiling",
        ee["mcap_reference_usd"] < ee["mcap_ceiling_usd"],
    )
    check(
        "early_entry junk floor is below the credit threshold",
        ee["junk_entry_liquidity_floor_usd"] <= ee["min_liquidity_usd_for_credit"],
    )
    check("early_entry age half-life is positive", ee["age_half_life_hours"] > 0)

    risk = cfg["risk"]
    check("risk hhi_ideal < hhi_ceiling", risk["hhi_ideal"] < risk["hhi_ceiling"])
    check("risk tail percentile is a low percentile", 0 < risk["tail_percentile"] < 50)

    ts = cfg["token_selection"]
    check("token_selection hit_rate_full_credit <= 1", 0 < ts["hit_rate_full_credit"] <= 1)
    check("token_selection benchmark cohort size is sane", ts["benchmark_min_cohort_n"] >= 2)

    ex = cfg["exit_effectiveness"]
    check(
        "exit observation windows are ascending",
        ex["observation_windows_hours"] == sorted(ex["observation_windows_hours"]),
    )
    check(
        "exit capture requires a positive opportunity (guards divide-by-zero and unfair zeros)",
        ex["capture_requires_positive_opportunity"] is True,
    )
    check(
        "exit adverse threshold is a fraction",
        0 < ex["adverse_exit_drawdown_pct"] < 1,
    )

    co = cfg["confidence"]
    sub = {k: v for k, v in co.items() if k.startswith("weight_")}
    check("confidence sub-weights sum to 1.0", abs(sum(sub.values()) - 1.0) < 1e-9, f"sum={sum(sub.values())!r}")

    return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        rc = 1
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        rc = 1
    else:
        print("ALL CONFIG CHECKS PASSED")
    raise SystemExit(rc)
