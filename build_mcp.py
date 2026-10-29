"""Build the deployable package of both advisors: dist/mcp/ and dist/mcp.zip.

  python build_mcp.py [--python-versions 3.13,3.12,3.14] [--no-wheelhouse]

Contents: the root docs, LICENSE and full_pipeline.ps1, and per advisor its
sources, scripts, tests, pipeline, config.toml (the repo's, with the server
machine's values from deploy_config.toml written in), MCP_INSTRUCTIONS.md, the C#
helpers (source + prebuilt Release binaries) and an offline win_amd64 wheelhouse.
Never included: .env, .index, .venv, *.db, caches, eval results, deploy_config.toml.
On the target machine: docs/SETUP.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
NAME = "mcp"

DEPLOY_CONFIG = ROOT / "deploy_config.toml"

ROOT_FILES = ["AGENTS.md", "README.md", "LICENSE", "full_pipeline.ps1"]
ROOT_DIRS: list[str] = []

ADVISORS = {
    "code_advisor": {
        "files": ["code_advisor_pipeline.ps1", "config.toml", "pyproject.toml", "MCP_INSTRUCTIONS.md", "README.md"],
        "dirs": ["src", "scripts", "tests"],
        # C# project -> the Release target frameworks it ships prebuilt
        "csharp": {"roslyn-host": ["net48", "net8.0"], "markup-codegen": ["net48"]},
        # setup.ps1 installs the package editable: the build backend has to be in the wheelhouse too
        "wheel_extra": ["editables"],
    },
    "db_advisor": {
        "files": ["db_advisor_pipeline.ps1", "config.toml", "pyproject.toml", "requirements.txt",
                  "MCP_INSTRUCTIONS.md", "README.md", ".env.example"],
        "dirs": ["src", "scripts", "tests", "knowledge"],
        "csharp": {"sql-parser": ["net8.0"], "code-extractor": ["net8.0"]},
        "wheel_extra": [],
    },
}

SKIP_DIRS = {"__pycache__", ".pytest_cache", "results", "results-v2", "bin", "obj", ".venv", ".index"}
SKIP_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm")
NEVER = (".env", ".index", ".venv")


def ignore(_dir, names):
    return [n for n in names if n in SKIP_DIRS or n.endswith(SKIP_SUFFIXES) or n == ".env"]


def copy_csharp(src: Path, dst: Path, tfms: list[str]) -> None:
    shutil.copytree(src, dst, ignore=ignore)
    for tfm in tfms:
        rel = src / "bin" / "Release" / tfm
        if not rel.is_dir():
            raise SystemExit(f"{rel} missing: dotnet build {src.name} -c Release")
        shutil.copytree(rel, dst / "bin" / "Release" / tfm)


def requirements(adv: Path, extra: list[str]) -> list[str]:
    """What setup.ps1 installs: requirements.txt when the advisor has one, else pyproject's
    dependencies + the dev extra + the build backend."""
    req = adv / "requirements.txt"
    if req.is_file():
        return [line.strip() for line in req.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
    pp = tomllib.loads((adv / "pyproject.toml").read_text(encoding="utf-8"))
    out = list(pp["project"]["dependencies"])
    out += pp["project"].get("optional-dependencies", {}).get("dev", [])
    out += pp.get("build-system", {}).get("requires", [])
    return out + extra


def flatten(table: dict, prefix: str = "") -> list[tuple[str, str, object]]:
    """(section, key, value) for every value in a deploy_config.toml advisor table."""
    out = []
    for k, v in table.items():
        if isinstance(v, dict):
            out += flatten(v, f"{prefix}.{k}" if prefix else k)
        else:
            out.append((prefix, k, v))
    return out


def apply_deploy_config(cfg: Path, table: dict) -> None:
    """Writes the server machine's values into a packaged config.toml line by line, so every
    other key and comment stays as in the repo.  A key the repo config lacks stops the build."""
    lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
    for section, key, value in flatten(table):
        if not isinstance(value, (str, bool, int)):
            raise SystemExit(f"{DEPLOY_CONFIG.name}: [{section}] {key}: only strings, booleans and integers")
        literal = json.dumps(value)  # a TOML literal too for str / bool / int
        current, hit = "", False
        for i, line in enumerate(lines):
            if h := re.match(r"\s*\[([^\]]+)\]\s*(#.*)?$", line):
                current = h.group(1).strip()
            elif current == section and (m := re.match(rf"(\s*{re.escape(key)}\s*=\s*)", line)):
                lines[i] = f"{m.group(1)}{literal}\n"
                hit = True
                break
        if not hit:
            raise SystemExit(f"{DEPLOY_CONFIG.name}: [{section}] {key} is not in {cfg.parent.name}/config.toml")
    text = "".join(lines)
    parsed = tomllib.loads(text)
    for section, key, value in flatten(table):
        node = parsed
        for part in section.split("."):
            node = node[part]
        if node[key] != value:
            raise SystemExit(f"{cfg}: [{section}] {key} did not take {value!r}")
    cfg.write_text(text, encoding="utf-8")


def wheelhouse(dst: Path, reqs: list[str], versions: list[str]) -> int:
    dst.mkdir()
    for v in versions:
        cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:", "--platform", "win_amd64",
               "--python-version", v, "--implementation", "cp", "-d", str(dst), *reqs]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            print(f"  wheelhouse for Python {v}: {p.stderr.strip()[-400:]}")
    return len(list(dst.glob("*.whl")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-versions", default="3.13,3.12,3.14")
    ap.add_argument("--no-wheelhouse", action="store_true", help="skip the offline wheelhouse (quick check)")
    a = ap.parse_args()
    versions = [v.strip() for v in a.python_versions.split(",") if v.strip()]
    deploy = tomllib.loads(DEPLOY_CONFIG.read_text(encoding="utf-8")) if DEPLOY_CONFIG.is_file() else {}
    if unknown := set(deploy) - set(ADVISORS):
        raise SystemExit(f"{DEPLOY_CONFIG.name}: unknown advisor(s) {sorted(unknown)}")

    out = DIST / NAME
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for f in ROOT_FILES:
        shutil.copy2(ROOT / f, out / f)
    for d in ROOT_DIRS:
        shutil.copytree(ROOT / d, out / d, ignore=ignore)

    for name, spec in ADVISORS.items():
        src, dst = ROOT / name, out / name
        dst.mkdir()
        for f in spec["files"]:
            shutil.copy2(src / f, dst / f)
        if name in deploy:
            apply_deploy_config(dst / "config.toml", deploy[name])
            print(f"{name}: config.toml with {len(flatten(deploy[name]))} value(s) from {DEPLOY_CONFIG.name}")
        for d in spec["dirs"]:
            shutil.copytree(src / d, dst / d, ignore=ignore)
        for proj, tfms in spec["csharp"].items():
            copy_csharp(src / proj, dst / proj, tfms)
        if not a.no_wheelhouse:
            n = wheelhouse(dst / "wheelhouse", requirements(src, spec["wheel_extra"]), versions)
            print(f"{name}: wheelhouse {n} appdb")

    bad = [p for p in out.rglob("*") if p.name in NEVER or p.name.endswith(SKIP_SUFFIXES)]
    if bad:
        raise SystemExit(f"refusing to package: {[str(p.relative_to(out)) for p in bad]}")

    z = DIST / f"{NAME}.zip"
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, Path("Senior-Dev-MCP") / f.relative_to(out))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{out}  ({size // 1024} KB)")
    print(f"{z}  ({z.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
