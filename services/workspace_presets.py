"""
Workspace LLM-Output-Validierung.

Seit dem Skript-Modell (2026-09-19): es gibt KEINE YAML-Spec und KEINEN
Dataset-Spec mehr — die Aufgabe besteht aus Dateien (Starter,
run.sh, test.sh, .solution/, .tests/) und flachen Umgebungsfeldern
(workspace_timeout/cpu/memory/internet/main_file).

Hier bleibt: `validate_workspace_generation()` — Validierung des
LLM-Outputs (unvertrauenswürdige Eingabe).
"""

import re

# z. B. "4g", "512m" — Einheit PFLICHT; passt zu _WS_MEM_RE (api/tutor.py)
_MEM_RE = re.compile(r"^\d+(\.\d+)?[bkmg]$")

# Keys der Engine/Image-Auswahl — im LLM-Output PFLICHT, sobald
# Umgebung/Dateien generiert werden (require_image_selection). Prompt-Aufruf
# (services/llm_service.py) und Validierung (api/tutor.py) teilen die Liste.
IMAGE_SELECTION_FIELDS = (
    "workspace_image", "workspace_engines", "proposed_image_spec")


def validate_workspace_generation(
    data: dict, allowed: set[str] | None = None,
) -> dict:
    """Validiert & bereinigt LLM-Output (unvertrauenswürdige Eingabe).

    Wirft ValueError mit UI-tauglicher Meldung. Gibt ein bereinigtes
    Dict NUR mit den tatsächlich übergebenen Schlüsseln zurück.
    allowed: Menge der zulässigen Top-Level-Schlüssel (= „ZULÄSSIGE FELDER"
    im Prompt). Übergebene Schlüssel außerhalb davon werden IGNORIERT
    (nicht validiert, nicht in `out`); None = alles zulassen.
    """
    import json as _json

    # Defensive: LLMs verschachteln die angeforderten Schlüssel teils in ein
    # gemeinsames Objekt („data“) oder liefern „env“ als JSON-STRING.
    # Beides vor der Validierung auf Top-Level glätten.
    inner = data.get("data")
    if isinstance(inner, dict):
        for k, v in inner.items():
            data.setdefault(k, v)
    if allowed is not None:
        # LLM darf nur die gelisteten Felder bearbeiten — alles andere wird
        # still ignoriert (auch nicht validiert, nicht zurückgegeben).
        data = {k: v for k, v in data.items() if k in allowed}
    if isinstance(data.get("env"), str):
        try:
            data["env"] = _json.loads(data["env"])
        except (ValueError, TypeError) as e:
            raise ValueError("env muss ein JSON-Objekt sein") from e

    out: dict = {}
    for field in ("title", "description", "model_solution"):
        if field in data:
            out[field] = str(data.get(field) or "").strip()[:50_000]

    if "env" in data:
        env = data.get("env")
        if env is None:
            out["env"] = None
        elif not isinstance(env, dict):
            raise ValueError("env muss ein Objekt sein")
        else:
            clean: dict = {}
            t = env.get("workspace_timeout")
            if t is not None:
                try:
                    t = int(t)
                except (TypeError, ValueError) as e:
                    raise ValueError("workspace_timeout muss eine Zahl sein") from e
                if not (1 <= t <= 7200):
                    raise ValueError("workspace_timeout muss zwischen 1 und 7200 liegen")
                clean["workspace_timeout"] = t
            c = env.get("workspace_cpu")
            if c is not None:
                try:
                    c = float(c)
                except (TypeError, ValueError) as e:
                    raise ValueError("workspace_cpu muss eine Zahl sein") from e
                if not (0 < c <= 32):
                    raise ValueError("workspace_cpu muss zwischen 0.1 und 32 liegen")
                clean["workspace_cpu"] = c
            m = env.get("workspace_memory")
            if m is not None:
                m = str(m).strip().lower().replace(" ", "")
                if re.match(r"^\d+(\.\d+)?$", m):
                    m += "g"  # GB-Zahl (LLM liefert 4 → "4g")
                if not _MEM_RE.match(m):
                    raise ValueError("workspace_memory hat ein ungültiges Format (Zahl in GB, z. B. 4)")
                clean["workspace_memory"] = m
            i = env.get("workspace_internet")
            if i is not None:
                clean["workspace_internet"] = bool(i)
            mf = env.get("workspace_main_file")
            if mf is not None:
                mf = str(mf).strip()
                if mf and (mf.startswith("/") or ".." in mf.split("/")):
                    raise ValueError("workspace_main_file muss ein relativer Pfad sein")
                clean["workspace_main_file"] = mf
            out["env"] = clean

    if "files" in data:
        files = data.get("files")
        if not isinstance(files, list):
            raise ValueError("files muss eine Liste sein")
        if len(files) > 20:
            raise ValueError("Zu viele Dateien (max. 20)")
        clean_files, seen, total = [], set(), 0
        for f in files:
            if not isinstance(f, dict):
                raise ValueError("Jede Datei muss {path, content|edits} sein")
            path = str(f.get("path") or "").strip().replace("\\", "/").lstrip("/")
            if not path or ".." in path.split("/") or path.endswith("/"):
                raise ValueError(f"Ungültiger Dateipfad: {path!r}")
            if path in seen:
                raise ValueError(f"Doppelter Dateipfad: {path!r}")
            seen.add(path)
            access = f.get("access")
            if access is not None:
                access = str(access).strip().lower()
                if access not in ("readonly", "hidden"):
                    raise ValueError(
                        f"Ungültiger access-Wert: {access!r} "
                        "(erlaubt: 'readonly' | 'hidden')")
            if "content" in f and "edits" in f:
                raise ValueError(
                    f"„{path}“: „content“ und „edits“ schließen sich aus "
                    "(pro Datei genau eines)")
            if "edits" in f:
                # Stellenseitige Bearbeitung einer BESTEHENDEN Datei
                edits = f.get("edits")
                if not isinstance(edits, list) or not edits:
                    raise ValueError(
                        f"„edits“ für „{path}“ muss eine nicht-leere Liste sein")
                if len(edits) > 20:
                    raise ValueError(f"Zu viele Edits für „{path}“ (max. 20)")
                clean_edits = []
                for ed in edits:
                    if not isinstance(ed, dict):
                        raise ValueError(f"Jedes Edit für „{path}“ muss ein Objekt sein")
                    if str(ed.get("op") or "") != "replace_span":
                        raise ValueError(
                            f"Unbekanntes Edit-op für „{path}“: {ed.get('op')!r} "
                            "(erlaubt: 'replace_span')")
                    old = str(ed.get("old") or "")
                    new = str(ed.get("new") or "")
                    if not old.strip():
                        raise ValueError(f"Edit für „{path}“ braucht „old“")
                    if len(old) > 20_000:
                        raise ValueError(f"„old“ für „{path}“ zu groß (>20 KB)")
                    if len(new) > 100_000:
                        raise ValueError(f"„new“ für „{path}“ zu groß (>100 KB)")
                    clean_edits.append({"op": "replace_span", "old": old, "new": new})
                total += sum(len(ed["new"]) for ed in clean_edits)
                if total > 1_000_000:
                    raise ValueError("Gesamtvolumen der Dateien zu groß (>1 MB)")
                clean_files.append({"path": path, "edits": clean_edits,
                                    **({"access": access} if access is not None else {})})
            else:
                content = str(f.get("content") or "")
                if len(content) > 100_000:
                    raise ValueError(f"Datei zu groß (>100 KB): {path!r}")
                total += len(content)
                if total > 1_000_000:
                    raise ValueError("Gesamtvolumen der Dateien zu groß (>1 MB)")
                clean_files.append({"path": path, "content": content,
                                    **({"access": access} if access is not None else {})})
        out["files"] = clean_files

    if "delete_files" in data:
        dels = data.get("delete_files")
        if not isinstance(dels, list):
            raise ValueError("delete_files muss eine Liste sein")
        file_paths = {f["path"] for f in out.get("files") or []}
        clean_dels, seen_dels = [], set()
        for p in dels:
            p = str(p or "").strip().replace("\\", "/").lstrip("/")
            if not p or ".." in p.split("/") or p.endswith("/"):
                raise ValueError(f"Ungültiger Löschpfad: {p!r}")
            if p in seen_dels:
                continue
            if p in file_paths:
                raise ValueError(
                    f"„{p}“ darf nicht gleichzeitig in „files“ und "
                    "„delete_files“ stehen.")
            seen_dels.add(p)
            clean_dels.append(p)
        out["delete_files"] = clean_dels

    if "folders" in data:
        folders = data.get("folders")
        if folders is None:
            out["folders"] = None
        elif not isinstance(folders, dict):
            raise ValueError("folders muss ein Objekt sein")
        else:
            clean_folders: dict[str, str] = {}
            for p, a in folders.items():
                p = str(p).strip().replace("\\", "/").lstrip("/")
                if not p or p.endswith("/") or ".." in p.split("/"):
                    raise ValueError(f"Ungültiger Ordnerpfad: {p!r}")
                a = str(a).strip().lower()
                if a not in ("readonly", "hidden"):
                    raise ValueError(
                        f"Ungültiger Ordner-Zugriff: {a!r} "
                        "(erlaubt: 'readonly' | 'hidden')")
                clean_folders[p] = a
            out["folders"] = clean_folders or None

    # Compute-Engine + Image-Spec (Phase 6):
    # Typ-Checks hier, Existenz-Checks in api/tutor.py (braucht DB-Session).
    if "workspace_image" in data:
        v = data.get("workspace_image")
        out["workspace_image"] = str(v).strip() if v else None

    if "workspace_engines" in data:
        v = data.get("workspace_engines")
        if v is None:
            out["workspace_engines"] = None
        else:
            if not isinstance(v, list):
                raise ValueError("workspace_engines muss eine Liste oder null sein")
            names = [str(x).strip() for x in v if str(x).strip()]
            out["workspace_engines"] = names or None

    if "proposed_image_spec" in data:
        v = data.get("proposed_image_spec")
        if v is None:
            out["proposed_image_spec"] = None
        else:
            if not isinstance(v, dict):
                raise ValueError("proposed_image_spec muss null oder ein Objekt sein")
            name = str(v.get("name") or "").strip()
            dockerfile = str(v.get("dockerfile") or "").strip()
            if not name:
                raise ValueError("proposed_image_spec braucht einen Namen (Slug)")
            if not dockerfile:
                raise ValueError("proposed_image_spec braucht ein dockerfile")
            # Validierung über das geteilte Agent-Modul (eine Quelle)
            from compute_agent.image_spec import (ImageSpecError, name_ok,
                                                  validate as _validate_img)
            if not name_ok(name):
                raise ValueError(
                    f"proposed_image_spec.name ungültiger Slug: {name!r}")
            try:
                _validate_img(dockerfile)
            except ImageSpecError as e:
                raise ValueError(f"proposed_image_spec.dockerfile ungültig: {e}") from e
            out["proposed_image_spec"] = {"name": name, "dockerfile": dockerfile}
    return out
