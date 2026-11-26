"""Keep the manifest of the Herdr App assets the wheel ships in step with the files.

    python3 tools/herdr_app_manifest.py check   # exit 1 and name every file whose row is stale
    python3 tools/herdr_app_manifest.py write   # rewrite the `files` rows; keep every other key

The rows carry the digest (`sha256:<hex>`, the notation OCI uses) and the size of each file
under `src/answerback_bridge/data/herdr-app/` (the manifest itself left out). The hand-written
keys of a row (`kind`, `licence`, `provenance`, `written_by`, `note`, `previous_digests`) are kept;
a new file gets a row with `kind` only, to be completed by hand. `previous_digests` lists the
earlier shipped versions of a file, which the installer updates (with a backup) instead of keeping
as a change of the person's.

The `superseded` key names the older panel (`integrations/herdr-app/answerback`, the one
`docs/HERDR-APP-MOD.md` described before T-181) by the digest of each of its files, so that the
installer can tell an unchanged copy of it from an edited one without shipping it. `write`
refreshes those digests too. `tests/test_herdr_app.py` runs the check.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from answerback_bridge.providers.herdr.herdr_app import manifest_path  # noqa: E402  (the installer's own check)

ROOT = Path(__file__).resolve().parents[1] / "src" / "answerback_bridge" / "data" / "herdr-app"
MANIFEST = ROOT / "manifest.json"
SUPERSEDED = Path(__file__).resolve().parents[1] / "integrations" / "herdr-app" / "answerback"
KEPT = ("kind", "licence", "provenance", "written_by", "note", "previous_digests")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def rows(root: Path = ROOT) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"):
        data = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        found.append(
            {
                "path": relative,
                "digest": digest(data),
                "bytes": len(data),
                "kind": relative.split("/", 1)[0].rstrip("s"),
            }
        )
    return found


def merged(manifest: dict[str, object], fresh: list[dict[str, object]]) -> list[dict[str, object]]:
    previous = {row["path"]: row for row in manifest.get("files", [])}  # type: ignore[union-attr]
    out: list[dict[str, object]] = []
    for row in fresh:
        old = previous.get(row["path"], {})
        kept = {key: old[key] for key in KEPT if key in old}
        out.append({**row, **kept})
    return out


def superseded_files(folder: Path = SUPERSEDED) -> dict[str, str]:
    """The older panel's files by digest; the App's helpers beside a mod are left out."""
    return {
        path.name: digest(path.read_bytes())
        for path in sorted(folder.iterdir())
        if path.is_file() and path.name not in ("gpui-kit.d.ts", "jsconfig.json")
    }


def stale(manifest: dict[str, object], fresh: list[dict[str, object]]) -> list[str]:
    wanted = {
        (row["path"], row["digest"], row["bytes"])
        for row in manifest.get("files", [])  # type: ignore[union-attr]
    }
    have = {(row["path"], row["digest"], row["bytes"]) for row in fresh}
    problems = sorted(str(path) for path, _, _ in wanted ^ have)
    superseded = manifest.get("superseded")
    if not isinstance(superseded, dict) or superseded.get("files") != superseded_files():
        problems.append("superseded: " + SUPERSEDED.as_posix())
    problems.extend(unsafe_paths(manifest))
    return problems


def unsafe_paths(manifest: dict[str, object]) -> list[str]:
    """Every path of the manifest the installer would refuse (`files[].path`, `superseded.path`):
    the installer's own rule, so the check and the install agree."""
    found: list[str] = []
    for row in manifest.get("files", []):  # type: ignore[union-attr]
        if manifest_path(row.get("path")) is None:
            found.append(f"path the installer refuses: files[].path {row.get('path')!r}")
    superseded = manifest.get("superseded")
    if isinstance(superseded, dict) and manifest_path(superseded.get("path")) is None:
        found.append(f"path the installer refuses: superseded.path {superseded.get('path')!r}")
    return found


def main(argv: list[str]) -> int:
    if argv[1:] not in (["check"], ["write"]):
        print(__doc__, file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    fresh = rows()
    if argv[1] == "check":
        problems = stale(manifest, fresh)
        for path in problems:
            print(f"stale or missing row: {path}")
        return 1 if problems else 0
    manifest["files"] = merged(manifest, fresh)
    superseded = manifest.setdefault("superseded", {})
    superseded.setdefault("path", "mods/answerback")  # type: ignore[union-attr]
    superseded["files"] = superseded_files()  # type: ignore[index]
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
