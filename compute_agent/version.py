"""Agent-Version: Content-Hash der eigenen Quelldateien.

Das AICampus-Backend berechnet über dieselbe Funktion denselben Hash
über seine `compute_agent/`-Kopie und vergleicht ihn mit dem Wert aus
dem `/health`-Report — so ist auf einen Blick sichtbar, welche Engines
veralteten Agent-Code fahren (z. B. nach einem Update von
`compute_agent/`, für das das Agent-Image nicht neu gebaut wurde).
Die Funktion liegt bewusst hier (und nicht im Backend), damit beide
Seiten immer exakt denselben Algorithmus nutzen.
"""
from __future__ import annotations

import hashlib
import pathlib

_VERSION: str | None = None


def _hashed_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Quelldateien, die das Agent-Verhalten definieren:
    Python-Module, Go-Quellcode des Relay (+ go.mod) und die
    Python-Abhängigkeiten. Build-Artefakte (Relay-Binary, __pycache__)
    werden bewusst ausgeschlossen — sie sind deterministisch aus den
    Quellen erzeugt."""
    out: list[pathlib.Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        if p.suffix in (".py", ".go") or p.name in ("go.mod", "requirements.txt"):
            out.append(p)
    return out


def agent_version() -> str:
    """sha256 über (Pfad, Inhalt) aller Agent-Quelldateien; 8 Hex-Zeichen."""
    global _VERSION
    if _VERSION is None:
        root = pathlib.Path(__file__).resolve().parent
        h = hashlib.sha256()
        for p in _hashed_files(root):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
        _VERSION = h.hexdigest()[:8]
    return _VERSION
