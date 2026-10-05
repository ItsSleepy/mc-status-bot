"""
EUGH modpack version watcher.

Reads every jar in the server's mods folder and pulls {modId: version}
out of the jar metadata rather than trusting filenames, which lie
constantly. Snapshots that map, and diffs two snapshots into a changelog.

Resolution order per jar:
  1. `version` in META-INF/neoforge.mods.toml (or mods.toml)
  2. Implementation-Version in META-INF/MANIFEST.MF  (covers ${file.jarVersion})
  3. last version-shaped token in the filename        (last resort)

Standalone use:
    python modpack.py scan
    python modpack.py snapshot 2.8
    python modpack.py diff 2.7 2.8
    python modpack.py list
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SERVER_DIR = Path("/home/sleepy/eugh")
DEFAULT_MODS_DIR = DEFAULT_SERVER_DIR / "mods"
DEFAULT_SNAPSHOT_DIR = Path(__file__).parent / "versions"

# nested jars are transitive libraries shaded in by jarjar, not pack content
NESTED_PREFIXES = ("META-INF/jarjar/",)

_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+[\w.+]*")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
_LOADER_NOISE = re.compile(r"(?i)[-_](neoforge|forge|fabric|quilt|all|bundled|universal|mc)")


# ---------- filename fallbacks ----------

def _clean_stem(stem: str) -> str:
    return _LOADER_NOISE.sub("-", stem)


def _version_from_filename(stem: str) -> str:
    tokens = _VERSION_TOKEN.findall(_clean_stem(stem))
    return tokens[-1] if tokens else "unknown"


def _modid_from_filename(stem: str) -> str:
    head = re.split(r"(?i)[-_](?=v?\d)", _clean_stem(stem), maxsplit=1)[0]
    return head.lower().strip("-_ ") or stem.lower()


# ---------- jar metadata ----------

def _manifest_version(z: zipfile.ZipFile) -> str | None:
    try:
        raw = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, OSError):
        return None
    # MANIFEST wraps long values onto continuation lines starting with a space
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n ", "")
    for line in raw.split("\n"):
        if line.lower().startswith("implementation-version:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _parse_mods_toml(data: bytes) -> dict:
    text = data.decode("utf-8-sig", "replace")
    try:
        mods = tomllib.loads(text).get("mods") or []
        if mods:
            return mods[0]
    except tomllib.TOMLDecodeError:
        pass  # some mods ship genuinely invalid toml — scrape it instead
    out: dict[str, str] = {}
    for key in ("modId", "version", "displayName"):
        m = re.search(rf'(?m)^\s*{key}\s*=\s*"([^"]*)"', text)
        if m:
            out[key] = m.group(1)
    return out


def scan_jar(path: Path) -> tuple[str, dict]:
    stem = path.stem
    meta: dict = {}
    version = ""
    try:
        with zipfile.ZipFile(path) as z:
            names = [
                n for n in z.namelist()
                if n.lower().endswith("mods.toml") and not n.startswith(NESTED_PREFIXES)
            ]
            if names:
                meta = _parse_mods_toml(z.read(names[0]))
            version = str(meta.get("version") or "")
            if not version or _PLACEHOLDER.search(version):
                version = _manifest_version(z) or ""
    except (zipfile.BadZipFile, OSError):
        version = ""

    if not version or _PLACEHOLDER.search(version):
        version = _version_from_filename(stem)

    mod_id = str(meta.get("modId") or "").strip()
    if not mod_id or _PLACEHOLDER.search(mod_id):
        mod_id = _modid_from_filename(stem)

    name = str(meta.get("displayName") or "").strip()
    if not name or _PLACEHOLDER.search(name):
        name = mod_id
    return mod_id, {"name": name, "version": version, "file": path.name}


def scan_mods(mods_dir=DEFAULT_MODS_DIR) -> dict:
    """{modId: {name, version, file}} for every jar in the folder."""
    mods: dict[str, dict] = {}
    for p in sorted(Path(mods_dir).glob("*.jar")):
        mod_id, info = scan_jar(p)
        if mod_id in mods:  # two jars claiming one id — keep both, disambiguated
            mod_id = f"{mod_id}:{p.stem.lower()}"
        mods[mod_id] = info
    return mods


def fingerprint(mods_dir=DEFAULT_MODS_DIR) -> str:
    """Cheap change detector — name/size/mtime only, no zip reads.

    Lets the bot poll often without opening 148 archives every time."""
    h = hashlib.sha256()
    for p in sorted(Path(mods_dir).glob("*.jar")):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def detect_loader(server_dir=DEFAULT_SERVER_DIR) -> str | None:
    for p in Path(server_dir).glob("neoforge-*-installer.jar"):
        m = re.search(r"neoforge-([\d.]+)-installer", p.name)
        if m:
            return f"NeoForge {m.group(1)}"
    return None


# ---------- snapshots + diff ----------

def save_snapshot(snapshot_dir, label: str, mods: dict, loader: str | None = None) -> Path:
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "loader": loader,
        "mod_count": len(mods),
        "mods": mods,
    }
    f = d / f"{label}.json"
    f.write_text(json.dumps(payload, indent=2))
    return f


def load_snapshot(snapshot_dir, label: str) -> dict | None:
    f = Path(snapshot_dir) / f"{label}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_snapshots(snapshot_dir) -> list[str]:
    d = Path(snapshot_dir)
    if not d.exists():
        return []
    return sorted((x.stem for x in d.glob("*.json")), key=_sort_key)


def _sort_key(label: str):
    parts = re.findall(r"\d+", label)
    return ([int(p) for p in parts], label)


def diff(old: dict, new: dict) -> dict:
    old_ids, new_ids = set(old), set(new)
    added = [
        {"id": i, "name": new[i]["name"], "version": new[i]["version"]}
        for i in sorted(new_ids - old_ids, key=lambda x: new[x]["name"].lower())
    ]
    removed = [
        {"id": i, "name": old[i]["name"], "version": old[i]["version"]}
        for i in sorted(old_ids - new_ids, key=lambda x: old[x]["name"].lower())
    ]
    updated = [
        {"id": i, "name": new[i]["name"], "old": old[i]["version"], "new": new[i]["version"]}
        for i in sorted(old_ids & new_ids, key=lambda x: new[x]["name"].lower())
        if old[i]["version"] != new[i]["version"]
    ]
    return {"added": added, "removed": removed, "updated": updated}


def is_empty(d: dict) -> bool:
    return not (d["added"] or d["removed"] or d["updated"])


def summarise(d: dict) -> str:
    bits = []
    if d["updated"]:
        bits.append(f"{len(d['updated'])} updated")
    if d["added"]:
        bits.append(f"{len(d['added'])} added")
    if d["removed"]:
        bits.append(f"{len(d['removed'])} removed")
    return ", ".join(bits) if bits else "no changes"


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "scan"

    if cmd == "scan":
        mods = scan_mods()
        for mod_id, info in sorted(mods.items()):
            print(f"{mod_id:<40} {info['version']}")
        print(f"\n{len(mods)} mods · {detect_loader() or 'loader unknown'}")
        return 0

    if cmd == "snapshot":
        if len(argv) < 2:
            print("usage: modpack.py snapshot <label>")
            return 2
        mods = scan_mods()
        f = save_snapshot(DEFAULT_SNAPSHOT_DIR, argv[1], mods, detect_loader())
        print(f"wrote {f} ({len(mods)} mods)")
        return 0

    if cmd == "list":
        for label in list_snapshots(DEFAULT_SNAPSHOT_DIR):
            snap = load_snapshot(DEFAULT_SNAPSHOT_DIR, label) or {}
            print(f"{label:<12} {snap.get('mod_count', '?')} mods   {snap.get('created', '')}")
        return 0

    if cmd == "diff":
        if len(argv) < 3:
            print("usage: modpack.py diff <from> <to>")
            return 2
        a = load_snapshot(DEFAULT_SNAPSHOT_DIR, argv[1])
        b = load_snapshot(DEFAULT_SNAPSHOT_DIR, argv[2])
        if not a or not b:
            print("one or both snapshots not found")
            return 1
        d = diff(a["mods"], b["mods"])
        print(f"{argv[1]} -> {argv[2]}: {summarise(d)}\n")
        for m in d["updated"]:
            print(f"  ~ {m['name']}  {m['old']} -> {m['new']}")
        for m in d["added"]:
            print(f"  + {m['name']}  {m['version']}")
        for m in d["removed"]:
            print(f"  - {m['name']}  {m['version']}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
