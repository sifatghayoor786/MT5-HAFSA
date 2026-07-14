"""Environment inspection (`doctor`).

Reports what this host can and cannot do. Never prints credential values —
only presence/absence of the environment variables.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DoctorCheck:
    name: str
    status: str  # PASS | FAIL | INFO | USER-ACTION
    detail: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def sim_only(self) -> bool:
        return not any(c.name == "mt5_package" and c.status == "PASS" for c in self.checks)

    def render(self) -> str:
        lines = ["AEGIS VELOCITY doctor"]
        lines += [f"  [{c.status:>11}] {c.name}: {c.detail}" for c in self.checks]
        mode = "SIM-ONLY (SimMt5Client)" if self.sim_only else "REAL-TERMINAL capable"
        lines.append(f"  execution path: {mode}")
        return "\n".join(lines)


def run_doctor(repo_root: Path | None = None) -> DoctorReport:
    root = repo_root if repo_root is not None else Path.cwd()
    rep = DoctorReport()

    bits = struct.calcsize("P") * 8
    py_ok = sys.version_info >= (3, 11) and bits == 64
    rep.checks.append(
        DoctorCheck(
            "python",
            "PASS" if py_ok else "FAIL",
            f"{platform.python_version()} {bits}-bit on {sys.platform}",
        )
    )

    is_windows = sys.platform == "win32"
    rep.checks.append(
        DoctorCheck(
            "platform",
            "PASS" if is_windows else "INFO",
            platform.platform()
            + ("" if is_windows else " — MT5 terminal requires Windows; sim-only here"),
        )
    )

    has_mt5 = importlib.util.find_spec("MetaTrader5") is not None
    rep.checks.append(
        DoctorCheck(
            "mt5_package",
            "PASS" if has_mt5 else "USER-ACTION",
            "MetaTrader5 importable"
            if has_mt5
            else "not installed (Windows-only); on the trading host: pip install MetaTrader5",
        )
    )

    term = os.environ.get("MT5_TERMINAL_PATH", "")
    if term and Path(term).exists():
        rep.checks.append(DoctorCheck("terminal", "PASS", f"found at {term}"))
        editor = Path(term).parent / "metaeditor64.exe"
        rep.checks.append(
            DoctorCheck(
                "metaeditor",
                "PASS" if editor.exists() else "USER-ACTION",
                str(editor) if editor.exists() else "metaeditor64.exe not found beside terminal",
            )
        )
    else:
        rep.checks.append(
            DoctorCheck(
                "terminal",
                "USER-ACTION",
                "MT5_TERMINAL_PATH unset or missing — set it in .env on the trading host",
            )
        )

    for var in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        present = bool(os.environ.get(var))
        rep.checks.append(
            DoctorCheck(
                var.lower(),
                "PASS" if present else "USER-ACTION",
                "set (value not shown)" if present else "not set — fill .env from .env.example",
            )
        )

    cfg = root / "configs"
    have = sorted(p.name for p in cfg.glob("*.yaml")) if cfg.is_dir() else []
    rep.checks.append(
        DoctorCheck(
            "configs",
            "PASS" if have else "FAIL",
            ", ".join(have) if have else "configs/ missing",
        )
    )

    data = root / "data"
    try:
        data.mkdir(exist_ok=True)
        probe = data / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        rep.checks.append(DoctorCheck("data_dir", "PASS", f"writable: {data}"))
    except OSError as exc:  # pragma: no cover - environment dependent
        rep.checks.append(DoctorCheck("data_dir", "FAIL", f"not writable: {exc}"))

    return rep
