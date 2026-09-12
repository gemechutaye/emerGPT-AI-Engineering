#!/usr/bin/env python3
"""Plan a byte-preserving release export; copying requires a matching frozen plan.

This never deletes source/destination files or copies Git history. See
docs/RELEASE_CONTENTS.md before applying a plan from an active checkout.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tempfile


SCHEMA = 1
TREES = (
    "apps/api/src", "apps/api/tests", "apps/api/migrations", "apps/web/src",
    "apps/web/public", "apps/web/scripts", "evals", "config", "tests",
)
FILES = (
    "pyproject.toml", "uv.lock", "alembic.ini",
    "EMER_AI_TakeHome_Knowledge_Corpus.jsonl", "EMER_AI_TakeHome_Data_README.txt",
    "config/corpus.json", "config/model-selection.json", "config/evaluation-summary.json",
    "apps/web/package.json", "apps/web/package-lock.json", "apps/web/tsconfig.json",
    "apps/web/vite.config.ts", "apps/web/vitest.config.ts", "apps/web/.prettierignore",
    "apps/web/index.html", "apps/web/openapi.json", "apps/web/THIRD_PARTY_NOTICES.md",
    "docs/ASSIGNMENT_ARCHITECTURE.md", "docs/LIVE_IMPLEMENTATION.md",
)
PINNED_ORIGINALS = {
    "EMER_AI_TakeHome_Knowledge_Corpus.jsonl": "b66264c95a383b2169650083ca85bd9f50cf3929b838ac7790ff6b6049a6901a",
    "EMER_AI_TakeHome_Data_README.txt": "047014e72061e448e9cdc3de5264cca5ab6ffa0a09bed17fa2fadd0aaee78ca4",
}
RECORDED = "apps/api/tests/fixtures/recorded-retrieval"
UI_EVIDENCE = "artifacts/verification/final-ui-acceptance-20260912"
INTELLIGENCE = "artifacts/verification/intelligence-20260912"
RELOCATIONS = {
    f"{INTELLIGENCE}/final-blind-http/FBA-002.T1/run.json": f"{RECORDED}/compound-question-run.json",
    f"{INTELLIGENCE}/retrieval-audit/fts-v2-large.sqlite": f"{RECORDED}/fts-v2-large.sqlite",
    "artifacts/verification/embeddings-large-20260911/openai-text-embedding-3-large.sqlite": f"{RECORDED}/legacy-large.sqlite",
    f"{INTELLIGENCE}/source-mutations/freeze.json": "evals/intelligence-source-mutations-20260912.freeze.json",
}
EVIDENCE = (
    "all-35-ui-sources.json", "ui-run-audit.json", "live-end-retest-audit.json",
    "final-record-recap-independent-review.json", "backend-final-after-fixture-result.json",
    "web-final-idle-retest.txt", "final-acceptance-manifest.json", "acceptance-receipts.json",
)
PATCH_FILES = (
    "apps/api/tests/test_hybrid_retrieval_fusion.py",
    "apps/api/tests/test_procedure_reference_retrieval.py",
    "apps/api/tests/test_intelligence_retrieval.py", "scripts/intelligence_mutations.py",
)
IGNORED_PARTS = {".git", ".local", ".venv", "node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", "test-results", "playwright-report"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".dump", ".tsbuildinfo"}


def encoded(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {"..", ".git"} for part in path.parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlink is not exportable: {relative}")
    return current


def allowed(path: Path) -> bool:
    return not (
        set(path.parts) & IGNORED_PARTS or path.suffix in FORBIDDEN_SUFFIXES
        or path.name.startswith(".env") or path.name in {"runtime.env", ".DS_Store"}
    )


def row(source: Path, original: str, destination: str) -> dict:
    path = safe_path(source, original)
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"Source changed during inspection: {original}")
    checksum = digest(data)
    if original in PINNED_ORIGINALS and checksum != PINNED_ORIGINALS[original]:
        raise ValueError(f"Supplied original checksum differs: {original}")
    return {"source": original, "destination": destination, "bytes": len(data), "sha256": checksum,
            "mode": "0755" if before.st_mode & stat.S_IXUSR else "0644"}


def plan(source: Path, preserve: list[str]) -> dict:
    for original in PINNED_ORIGINALS:
        row(source, original, original)
    mapping = {path: path for path in FILES}
    for tree in TREES:
        base = safe_path(source, tree)
        if not base.is_dir():
            raise ValueError(f"Required source directory is missing: {tree}")
        for path in base.rglob("*"):
            relative = path.relative_to(source)
            if allowed(relative) and (path.is_file() or path.is_symlink()):
                mapping[relative.as_posix()] = relative.as_posix()
    for path in (source / "scripts").iterdir():
        if path.suffix in {".py", ".sh"} and allowed(path.relative_to(source)):
            mapping[path.relative_to(source).as_posix()] = path.relative_to(source).as_posix()
    mapping.update(RELOCATIONS)
    for name in EVIDENCE:
        mapping[f"{UI_EVIDENCE}/{name}"] = f"docs/evidence/local-acceptance-20260912/{name}"
    for relative in preserve:
        safe_path(source, relative)
    entries = [row(source, original, destination) for original, destination in sorted(mapping.items())
               if not any(destination == p or destination.startswith(p.rstrip("/") + "/") for p in preserve)]
    if len({entry["destination"] for entry in entries}) != len(entries):
        raise ValueError("Multiple source files map to one destination")
    # Portable audit inventory only; raw experiment bytes remain in the original checkout.
    archive = []
    for path in sorted((source / "artifacts/verification").rglob("*")):
        if path.is_file() and allowed(path.relative_to(source)):
            entry = row(source, path.relative_to(source).as_posix(), "")
            archive.append({key: entry[key] for key in ("source", "bytes", "sha256")})
    return {"schema": SCHEMA, "preserved_destination_paths": sorted(preserve), "files": entries,
            "files_sha256": digest(encoded(entries)), "archive_files": archive,
            "archive_sha256": digest(encoded(archive)),
            "source_commit": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()}


def relocation_patch(source: Path) -> str:
    output = []
    for relative in PATCH_FILES:
        before = safe_path(source, relative).read_text()
        after = before
        for old, new in RELOCATIONS.items():
            after = after.replace(old, new)
        if before == after:
            raise ValueError(f"Expected fixture path was not found: {relative}")
        output.extend(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                           fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    return "".join(output)


def write_new(path: Path, data: bytes, mode: int = 0o644) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == data:
            return False
        raise ValueError(f"Existing destination differs; nothing may overwrite it: {path}")
    with path.open("xb") as handle:
        handle.write(data)
    path.chmod(mode)
    return True


def apply(source: Path, destination: Path, approved: dict) -> int:
    if plan(source, approved["preserved_destination_paths"]) != approved:
        raise ValueError("Source differs from approved plan; freeze a new plan at a stable checkpoint")
    # Preflight every destination before writing any application file.
    for entry in approved["files"]:
        target = safe_path(destination, entry["destination"])
        if target.exists() and digest(target.read_bytes()) != entry["sha256"]:
            raise ValueError(f"Existing destination differs: {entry['destination']}; use --preserve in a new plan")
    generated = {
        "docs/evidence/release-file-manifest.json": encoded({k: v for k, v in approved.items() if k != "archive_files"}),
        "docs/evidence/development-archive-manifest.json": encoded({"schema": SCHEMA, "status": "retained locally; not published", "root": "original development checkout", "files": approved["archive_files"], "sha256": approved["archive_sha256"]}),
    }
    for relative, data in generated.items():
        target = safe_path(destination, relative)
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing generated manifest differs: {relative}")
    # Stage exact verified bytes before promotion; re-inspect source to catch concurrent edits.
    with tempfile.TemporaryDirectory(prefix="emer-release-stage-") as temp:
        staging = Path(temp)
        for entry in approved["files"]:
            data = safe_path(source, entry["source"]).read_bytes()
            if digest(data) != entry["sha256"]:
                raise ValueError(f"Source changed while staging: {entry['source']}")
            write_new(staging / entry["destination"], data, int(entry["mode"], 8))
        if plan(source, approved["preserved_destination_paths"]) != approved:
            raise ValueError("Source changed during staging; no application files were promoted")
        copied = 0
        for entry in approved["files"]:
            target = safe_path(destination, entry["destination"])
            copied += write_new(target, (staging / entry["destination"]).read_bytes(), int(entry["mode"], 8))
        for relative, data in generated.items():
            write_new(safe_path(destination, relative), data)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, required=True, help="Dry-run output; mandatory frozen input with --apply")
    parser.add_argument("--preserve", action="append", default=[], metavar="RELATIVE_PATH", help="Exclude a release-owned destination file/subtree from the plan")
    parser.add_argument("--patch-out", type=Path, help="Write a proposed fixture-path patch; never apply it")
    parser.add_argument("--apply", action="store_true", help="Copy only when the current source matches the complete frozen manifest")
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate, non-nested directories")
    if not (destination / ".git").is_dir():
        raise ValueError("Destination must be the existing release clone; this tool never creates a Git repository")
    if args.apply:
        if args.preserve or args.patch_out:
            raise ValueError("Apply reads the frozen plan; --preserve and --patch-out are dry-run options")
        approved = json.loads(args.manifest.read_text())
        print(json.dumps({"mode": "applied", "copied": apply(source, destination, approved), "files_sha256": approved["files_sha256"]}))
    else:
        current = plan(source, args.preserve)
        write_new(args.manifest, encoded(current))
        if args.patch_out:
            write_new(args.patch_out, relocation_patch(source).encode())
        print(json.dumps({"mode": "dry-run", "files": len(current["files"]), "bytes": sum(row["bytes"] for row in current["files"]),
                          "files_sha256": current["files_sha256"], "archived_files": len(current["archive_files"]), "application_files_copied": 0}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Release preparation stopped: {error}") from error
