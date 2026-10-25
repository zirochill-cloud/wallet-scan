"""Configuration loading.

All tunables live in ``config/`` as YAML so that weights, thresholds and budget
caps are reviewable in one place and versioned alongside the code that uses
them (doc 05, config/budget.yaml). Nothing in the codebase should hardcode a
threshold that a human is expected to tune.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only in bare envs
    raise ImportError(
        "PyYAML is required to load solscope configuration. "
        "Install with: pip install pyyaml"
    ) from exc

CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ProviderBudget:
    """Monthly spend limits and per-operation costs for one data provider."""

    name: str
    monthly_cap: int
    warn_ratio: float = 0.8
    operation_costs: dict[str, int] = field(default_factory=dict)

    def cost_of(self, operation: str) -> int:
        """Credits charged per unit of ``operation``.

        Unknown operations are a hard error rather than a silent zero: charging
        nothing for an unrecognised call is exactly how a budget stops meaning
        anything.
        """
        try:
            return self.operation_costs[operation]
        except KeyError:
            known = ", ".join(sorted(self.operation_costs)) or "(none defined)"
            raise ConfigError(
                f"provider {self.name!r} has no cost defined for operation "
                f"{operation!r}; known operations: {known}"
            ) from None


@dataclass(frozen=True)
class BudgetConfig:
    providers: dict[str, ProviderBudget]
    fail_closed_on_ledger_error: bool = True

    def provider(self, name: str) -> ProviderBudget:
        try:
            return self.providers[name]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "(none defined)"
            raise ConfigError(
                f"unknown provider {name!r}; configured providers: {known}"
            ) from None


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def load_budget(path: pathlib.Path | None = None) -> BudgetConfig:
    """Load and validate ``config/budget.yaml``.

    Validation is deliberately strict. A cap of 0, a negative cost, or a
    warn_ratio outside (0, 1] are all rejected here rather than producing a
    ledger that silently permits unlimited spend.
    """
    path = path or (CONFIG_DIR / "budget.yaml")
    if not path.exists():
        raise ConfigError(f"budget config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    providers_raw = _require(raw, "providers", str(path))
    if not isinstance(providers_raw, dict) or not providers_raw:
        raise ConfigError(f"{path}: 'providers' must be a non-empty mapping")

    providers: dict[str, ProviderBudget] = {}
    for name, spec in providers_raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{path}: provider {name!r} must be a mapping")

        # A provider is either credit-based (helius) or request-based
        # (dexscreener). Accept either spelling of the cap.
        cap = spec.get("monthly_cap_credits", spec.get("monthly_cap_requests"))
        if cap is None:
            raise ConfigError(
                f"{path}: provider {name!r} needs monthly_cap_credits "
                f"or monthly_cap_requests"
            )
        if not isinstance(cap, int) or cap <= 0:
            raise ConfigError(
                f"{path}: provider {name!r} cap must be a positive integer, got {cap!r}"
            )

        warn_ratio = spec.get("warn_ratio", 0.8)
        if not isinstance(warn_ratio, (int, float)) or not (0 < warn_ratio <= 1):
            raise ConfigError(
                f"{path}: provider {name!r} warn_ratio must be in (0, 1], got {warn_ratio!r}"
            )

        costs = spec.get("operation_costs") or {}
        if not isinstance(costs, dict):
            raise ConfigError(f"{path}: provider {name!r} operation_costs must be a mapping")
        for op, cost in costs.items():
            if not isinstance(cost, int) or cost < 0:
                raise ConfigError(
                    f"{path}: provider {name!r} operation {op!r} cost must be "
                    f"a non-negative integer, got {cost!r}"
                )

        providers[name] = ProviderBudget(
            name=name,
            monthly_cap=cap,
            warn_ratio=float(warn_ratio),
            operation_costs=dict(costs),
        )

    fail_closed = raw.get("fail_closed_on_ledger_error", True)
    if not isinstance(fail_closed, bool):
        raise ConfigError(f"{path}: fail_closed_on_ledger_error must be a boolean")

    return BudgetConfig(providers=providers, fail_closed_on_ledger_error=fail_closed)


@dataclass(frozen=True)
class CollectorConfig:
    """Phase 1 forward-collector tunables (config/collector.yaml)."""

    job: str
    data_dir: pathlib.Path
    discovery: dict[str, Any]
    filter: dict[str, Any]
    subscription: dict[str, Any]
    metering: dict[str, Any]
    checkpoint: dict[str, Any]


def load_collector(path: pathlib.Path | None = None) -> CollectorConfig:
    """Load and validate ``config/collector.yaml``.

    Only the keys the collector depends on are validated structurally here;
    numeric plausibility (non-negative caps, positive intervals) is checked
    because a zero interval is an unbounded polling loop.
    """
    path = path or (CONFIG_DIR / "collector.yaml")
    if not path.exists():
        raise ConfigError(f"collector config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    discovery = _require(raw, "discovery", str(path))
    filt = _require(raw, "filter", str(path))
    subscription = _require(raw, "subscription", str(path))
    metering = _require(raw, "metering", str(path))
    checkpoint = raw.get("checkpoint") or {}

    for section in (discovery, filt, subscription, metering):
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: each section must be a mapping")

    for key in ("geckoterminal_poll_seconds", "dexscreener_poll_seconds"):
        val = discovery.get(key)
        if not isinstance(val, (int, float)) or val <= 0:
            raise ConfigError(f"{path}: discovery.{key} must be > 0, got {val!r}")

    for key in ("min_liquidity_usd", "min_age_hours"):
        val = filt.get(key)
        if not isinstance(val, (int, float)) or val < 0:
            raise ConfigError(f"{path}: filter.{key} must be >= 0, got {val!r}")
    min_txns = filt.get("min_txns_h1")
    if min_txns is not None and (not isinstance(min_txns, int) or min_txns < 0):
        raise ConfigError(f"{path}: filter.min_txns_h1 must be >= 0 or null, got {min_txns!r}")

    for key in ("max_pools", "subscriptions_per_connection", "max_connections"):
        val = subscription.get(key)
        if not isinstance(val, int) or val <= 0:
            raise ConfigError(f"{path}: subscription.{key} must be a positive integer, got {val!r}")

    for key in ("wss_bytes_per_unit", "wss_credits_per_unit", "wss_connection_credits"):
        val = metering.get(key)
        if not isinstance(val, int) or val <= 0:
            raise ConfigError(f"{path}: metering.{key} must be a positive integer, got {val!r}")

    overlap = checkpoint.get("overlap_slots", 64)
    if not isinstance(overlap, int) or overlap <= 0:
        raise ConfigError(f"{path}: checkpoint.overlap_slots must be a positive integer")

    data_dir = pathlib.Path(raw.get("data_dir", "data/raw"))
    return CollectorConfig(
        job=str(raw.get("job", "collect")),
        data_dir=data_dir,
        discovery=discovery,
        filter=filt,
        subscription=subscription,
        metering=metering,
        checkpoint={**checkpoint, "overlap_slots": overlap},
    )


def load_scoring(path: pathlib.Path | None = None) -> dict:
    """Load ``config/scoring.yaml``. Validated separately by tests/test_scoring_config.py."""
    path = path or (CONFIG_DIR / "scoring.yaml")
    if not path.exists():
        raise ConfigError(f"scoring config not found: {path}")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
