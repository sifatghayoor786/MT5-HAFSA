"""Symbol truth (§4.1): broker-name discovery and MEASURED scalp eligibility."""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from aegis_velocity.core.config import ScalpEligibilityCfg
from aegis_velocity.mt5.protocol import SymbolSpec


@dataclass(frozen=True)
class DiscoveryResult:
    mapping: dict[str, str]  # canonical -> broker name
    ambiguous: dict[str, list[str]]  # canonical -> candidates needing confirmation
    missing: list[str]


def _normalize(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def discover_symbols(broker_names: tuple[str, ...], universe: list[str]) -> DiscoveryResult:
    """Match canonical names (EURUSD) to broker names (EURUSDm, frxEURUSD, ...).

    Exact match wins; otherwise normalized containment with the shortest candidate
    preferred. Multiple equally-short candidates are reported as ambiguous, never
    guessed.
    """
    mapping: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    missing: list[str] = []
    normalized = {b: _normalize(b) for b in broker_names}
    for canonical in universe:
        canon = _normalize(canonical)
        if canonical in broker_names:
            mapping[canonical] = canonical
            continue
        candidates = [b for b, norm in normalized.items() if canon in norm]
        if not candidates:
            missing.append(canonical)
            continue
        shortest = min(len(_normalize(c)) for c in candidates)
        best = sorted(c for c in candidates if len(_normalize(c)) == shortest)
        if len(best) == 1:
            mapping[canonical] = best[0]
        else:
            ambiguous[canonical] = best
    return DiscoveryResult(mapping=mapping, ambiguous=ambiguous, missing=missing)


def persist_symbol_map(path: Path, mapping: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    tmp.replace(path)


def load_symbol_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


@dataclass(frozen=True)
class EligibilityVerdict:
    symbol: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    stops_level_points: int = 0
    spread_p50_points: float = 0.0
    ticks_per_minute: float = 0.0
    sample_ticks: int = 0


def measure_scalp_eligibility(
    spec: SymbolSpec,
    spread_samples_points: list[float],
    window_minutes: float,
    cfg: ScalpEligibilityCfg,
    min_samples: int = 50,
) -> EligibilityVerdict:
    """A symbol qualifies only on MEASURED evidence; thin samples fail closed."""
    reasons: list[str] = []
    n = len(spread_samples_points)
    if n < min_samples:
        reasons.append(f"INSUFFICIENT_SAMPLE: {n} ticks < {min_samples} required")
        return EligibilityVerdict(
            symbol=spec.name, eligible=False, reasons=reasons, sample_ticks=n,
            stops_level_points=spec.trade_stops_level,
        )
    spread_p50 = statistics.median(spread_samples_points)
    ticks_per_minute = n / window_minutes if window_minutes > 0 else 0.0

    if spec.trade_stops_level > cfg.max_stops_level_points:
        reasons.append(
            f"STOPS_LEVEL: {spec.trade_stops_level} > max {cfg.max_stops_level_points} points"
        )
    cap = cfg.spread_p50_cap(spec.name)
    if spread_p50 > cap:
        reasons.append(f"SPREAD_P50: measured {spread_p50:.1f} > cap {cap} points")
    if ticks_per_minute < cfg.min_ticks_per_minute:
        reasons.append(
            f"TICK_RATE: measured {ticks_per_minute:.1f}/min < {cfg.min_ticks_per_minute}/min"
        )
    if spec.trade_mode != "FULL":
        reasons.append(f"TRADE_MODE: {spec.trade_mode} != FULL")
    return EligibilityVerdict(
        symbol=spec.name,
        eligible=not reasons,
        reasons=reasons,
        stops_level_points=spec.trade_stops_level,
        spread_p50_points=spread_p50,
        ticks_per_minute=ticks_per_minute,
        sample_ticks=n,
    )


def persist_eligibility(path: Path, verdicts: list[EligibilityVerdict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        v.symbol: {
            "eligible": v.eligible,
            "reasons": v.reasons,
            "stops_level_points": v.stops_level_points,
            "spread_p50_points": v.spread_p50_points,
            "ticks_per_minute": v.ticks_per_minute,
            "sample_ticks": v.sample_ticks,
        }
        for v in verdicts
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
