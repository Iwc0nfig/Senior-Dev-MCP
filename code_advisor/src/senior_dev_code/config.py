"""Configuration loading.

``config.toml`` is the only place paths, limits, timeouts and server settings
live.  Everything else in the package takes a :class:`Config` instance; nothing
hard-codes a path.  Scripts read single values with
``python -m senior_dev_code.config get <dotted.key>``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_CONFIG_NAME = "config.toml"


def _norm(p: str | Path, base: Path | None = None) -> Path:
    """Absolute path with forward slashes, original case preserved.

    A relative path resolves against *base* (config.toml's folder), never the
    current directory, so one config works wherever the advisor is unpacked.
    """
    q = Path(str(p).replace("\\", "/"))
    if base is not None and not q.is_absolute():
        q = base / q
    return q.resolve()


def _norm_opt(p: str | Path | None, base: Path | None = None) -> Path | None:
    """As :func:`_norm`, but an absent value stays absent.

    ``_norm("")`` resolves to the current working directory, which turns "no
    Roslyn host configured" into a confusing "<cwd> not built".  On this server
    the hosts are deliberately unconfigured, so absent has to stay absent.
    """
    if p is None or not str(p).strip():
        return None
    return _norm(p, base)


@dataclass(frozen=True)
class Paths:
    workspaces: Path
    net48_solution: Path
    net8_solution: Path
    website_root: Path
    index_dir: Path
    # None when not configured: the graph and filesystem layers still answer.
    roslyn_host_net48: Path | None
    roslyn_host_net8: Path | None
    netfx_ref_assemblies: Path | None
    # The page-compiler bridge (markup-codegen/).  When absent, markup falls back to
    # the synthesised designer partials.
    markup_codegen: Path | None = None
    # The reference assemblies scripts/gen_aspnet_controls.ps1 reflects over.
    aspnet_controls_refdir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.index_dir / "graph.db"

    @property
    def build_dir(self) -> Path:
        """Everything the build step produces that is not inside the workspace."""
        return self.index_dir / "build"

    @property
    def markup_dir(self) -> Path:
        return self.build_dir / "markup"

    @property
    def build_manifest(self) -> Path:
        return self.build_dir / "manifest.json"

    @property
    def designer_dir(self) -> Path:
        return self.index_dir / "designer"

    @property
    def log_dir(self) -> Path:
        return self.index_dir / "logs"

    @property
    def aspnet_controls_json(self) -> Path:
        return self.index_dir / "aspnet_controls.json"


@dataclass(frozen=True)
class ServerOpts:
    """[server]: how ``python -m senior_dev_code`` serves; CLI flags override it.  Required."""

    transport: str
    host: str
    port: int
    path: str
    # The text the server sends clients as its MCP instructions.
    instructions_file: Path


@dataclass(frozen=True)
class Limits:
    roslyn_load_timeout_s: int = 900
    roslyn_query_timeout_s: int = 45
    max_results_default: int = 50
    max_results_hard: int = 500
    read_file_max_lines: int = 400
    # read_file_range: lines read from an anchor when no line_end is given
    read_anchor_window: int = 100
    # find_migration_twin caps max lower than max_results_hard
    twin_max_hard: int = 100
    # Default `max` per tool; a tool not listed uses max_results_default.
    tool_max: dict[str, int] = field(default_factory=dict)

    def max_for(self, tool: str) -> int:
        return self.tool_max.get(tool, self.max_results_default)


@dataclass(frozen=True)
class Timeouts:
    """Seconds.  The Roslyn load/query timeouts stay in [limits] where they always were."""

    sqlite_read_s: int = 30
    sqlite_write_s: int = 60
    roslyn_stop_wait_s: int = 5
    roslyn_ping_s: int = 10
    vswhere_s: int = 60
    refasm_download_s: int = 300


@dataclass(frozen=True)
class SourceExportOpts:
    """get_source_manifest / read_source_batch bounds."""

    max_file_bytes: int = 2 * 1024 * 1024
    manifest_limit_default: int = 20
    manifest_limit_max: int = 100
    batch_requests_max: int = 20
    budget_default: int = 8192
    budget_min: int = 2048
    budget_max: int = 65536


@dataclass(frozen=True)
class WorkspaceOpts:
    """Top-level folders of the workspace: which side a path is on."""

    net48_root: str = "Legacy"
    net8_root: str = "Modern"
    # Under net8_root: the "next" migration family (get_source_manifest)
    next_dir: str = "Modern.Next"


@dataclass(frozen=True)
class IndexOpts:
    max_file_bytes: int = 2_000_000
    # search_text with a path_glob names its files on purpose: those are searched up to this size
    search_glob_max_bytes: int = 20_000_000


@dataclass(frozen=True)
class BuildOpts:
    """How scripts/build-workspace.py builds the workspace copy."""

    configuration: str = "Debug"
    # Empty = find Visual Studio's MSBuild with vswhere.
    msbuild: str = ""
    # Parallel MSBuild processes per dependency level; 0 = CPU count.
    parallel: int = 0
    timeout_s: int = 1800
    # Where .NET Framework reference assemblies live when not installed as Developer Packs
    # (Microsoft.NETFramework.ReferenceAssemblies layout).  Passed to MSBuild and the Roslyn
    # load as TargetFrameworkRootPath.  load_config resolves it: empty = <index_dir>/build/refasm,
    # filled by the build step (build/refasm.py); relative = against config.toml's folder.
    target_framework_root: str = ""
    # Where Visual Studio / the Developer Packs install reference assemblies
    # (fallback when target_framework_root is unset, and the source of 4.7.2+).
    installed_ref_root: str = "C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework"
    vswhere: str = "C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"
    # Microsoft.NETFramework.ReferenceAssemblies.<tfm> packages for 4.5 - 4.7.1
    refasm_package_version: str = "1.0.3"
    refasm_url: str = "https://api.nuget.org/v3-flatcontainer/{id}/{v}/{id}.{v}.nupkg"
    # Global properties for every restore/build.  The defaults stop restore POLICY
    # (vulnerability audit, downgrade-as-error) failing a build whose code is fine;
    # the compiler sees no difference.
    properties: dict[str, str] = field(default_factory=lambda: {
        "NuGetAudit": "false",
        "TreatWarningsAsErrors": "false",
        "WarningsAsErrors": "",
        "RestorePackagesConfig": "true",
        # A feed that cannot be reached (nuget.fast-report.com, NU1301) otherwise
        # fails the whole restore, including packages every other feed has.
        "RestoreIgnoreFailedSources": "true",
    })


@dataclass(frozen=True)
class Config:
    paths: Paths
    limits: Limits
    index: IndexOpts
    build: BuildOpts = field(default_factory=BuildOpts)
    source: Path = field(default_factory=lambda: Path(DEFAULT_CONFIG_NAME))
    # None only for configs built by hand in tests; load_config requires [server].
    server: ServerOpts | None = None
    timeouts: Timeouts = field(default_factory=Timeouts)
    source_export: SourceExportOpts = field(default_factory=SourceExportOpts)
    workspace: WorkspaceOpts = field(default_factory=WorkspaceOpts)

    # ---- convenience -----------------------------------------------------

    def rel(self, p: Path | str) -> str:
        """Path relative to ``paths.workspaces``, forward slashes.

        Falls back to the absolute posix form for paths outside the workspace
        (synthetic designer files live under ``.index/``).
        """
        pp = Path(str(p).replace("\\", "/"))
        try:
            return pp.resolve().relative_to(self.paths.workspaces).as_posix()
        except ValueError:
            return pp.as_posix()

    def abs_of(self, rel_path: str) -> str:
        """Absolute posix path for a workspace-relative path stored in the index.

        The inverse of :meth:`rel`.  The index stores workspace-relative paths so it is portable
        between machines; the tool layer makes one absolute only when handing it to a caller
        or opening a file.
        """
        return (self.paths.workspaces / rel_path).as_posix()

    def synth_abs(self, name: str) -> str:
        """Absolute path of a synthesised designer partial from its stored name.

        ``pages.synth_path`` stores the basename only; the directory is always
        ``paths.designer_dir``.
        """
        return (self.paths.designer_dir / name).as_posix()

    def abs_posix(self, p: Path | str) -> str:
        return PurePosixPath(str(Path(str(p).replace("\\", "/")))).as_posix()

    def side_of(self, path: Path | str) -> str:
        """``net8`` for the Modern workspace, ``net48`` otherwise.

        Deliberately based on the top-level folder, never on namespaces -- the
        ``Modern`` *namespace* exists on both sides.
        """
        pp = Path(str(path).replace("\\", "/"))
        try:
            parts = pp.resolve().relative_to(self.paths.workspaces).parts
        except ValueError:
            return "net48"
        return "net8" if parts and parts[0].lower() == self.workspace.net8_root.lower() else "net48"

    def ensure_index_dirs(self) -> None:
        """The only directories this process is ever allowed to create."""
        self.paths.index_dir.mkdir(parents=True, exist_ok=True)
        self.paths.designer_dir.mkdir(parents=True, exist_ok=True)
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        self.paths.build_dir.mkdir(parents=True, exist_ok=True)


def find_config(start: Path | None = None) -> Path:
    """Walk up from *start* (or this file) looking for ``config.toml``."""
    here = (start or Path(__file__).resolve()).resolve()
    for d in [here, *here.parents]:
        cand = d / DEFAULT_CONFIG_NAME
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"{DEFAULT_CONFIG_NAME} not found above {here}. "
        "It is required: the advisor never guesses paths."
    )


def load_config(path: str | Path | None = None) -> Config:
    """Load ``config.toml``.

    Resolution order: the explicit *path*, then ``$SENIOR_DEV_CONFIG``, then a walk
    up from this file.  The env var matters when the package is not installed
    editable from the deployment directory, because then ``__file__`` sits in
    site-packages and the walk finds nothing (or the wrong file).  Relative paths
    in the file resolve against its folder.
    """
    if path is None:
        env = os.environ.get("SENIOR_DEV_CONFIG", "").strip()
        if env:
            path = env

    cfg_path = Path(path) if path else find_config()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"{cfg_path} does not exist.")
    with cfg_path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    p = raw.get("paths", {})
    missing = [
        k
        for k in (
            "workspaces",
            "net48_solution",
            "net8_solution",
            "website_root",
            "index_dir",
        )
        if k not in p
    ]
    if missing:
        raise ValueError(f"{cfg_path}: [paths] missing required keys: {missing}")
    base = cfg_path.resolve().parent

    paths = Paths(
        workspaces=_norm(p["workspaces"], base),
        net48_solution=_norm(p["net48_solution"], base),
        net8_solution=_norm(p["net8_solution"], base),
        website_root=_norm(p["website_root"], base),
        index_dir=_norm(p["index_dir"], base),
        roslyn_host_net48=_norm_opt(p.get("roslyn_host_net48"), base),
        roslyn_host_net8=_norm_opt(p.get("roslyn_host_net8"), base),
        netfx_ref_assemblies=_norm_opt(p.get("netfx_ref_assemblies"), base),
        markup_codegen=_norm_opt(p.get("markup_codegen"), base),
        aspnet_controls_refdir=_norm_opt(p.get("aspnet_controls_refdir"), base),
    )
    s = raw.get("server")
    if s is None:
        raise ValueError(f"{cfg_path}: [server] section missing")
    server = ServerOpts(
        transport=s["transport"], host=s["host"], port=int(s["port"]), path=s["path"],
        instructions_file=_norm(s.get("instructions_file", "MCP_INSTRUCTIONS.md"), base),
    )
    # Validate the root the whole index derives from: a typo in `workspaces` would
    # otherwise walk an empty tree and yield a silently empty index (the advisor never guesses paths).
    for key, value in (("workspaces", paths.workspaces), ("website_root", paths.website_root)):
        if not value.is_dir():
            raise NotADirectoryError(
                f"{cfg_path}: [paths] {key} = {value} is not a directory. "
                "Point it at the copy of the workspace on this machine."
            )

    return Config(
        paths=paths,
        limits=Limits(**raw.get("limits", {})),
        index=IndexOpts(**raw.get("index", {})),
        build=_build_opts(raw.get("build", {}), cfg_path.parent, paths.index_dir),
        source=cfg_path,
        server=server,
        timeouts=Timeouts(**raw.get("timeouts", {})),
        source_export=SourceExportOpts(**raw.get("source_export", {})),
        workspace=WorkspaceOpts(**raw.get("workspace", {})),
    )


def lookup(cfg: Any, key: str) -> Any:
    """``cfg.<a>.<b>`` for the dotted *key* ``a.b``, e.g. ``server.port`` or ``paths.db_path``."""
    obj = cfg
    for part in key.split("."):
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def render_instructions(cfg: Config) -> str:
    """The MCP instructions: [server] instructions_file with ``{{dotted.key}}`` filled from *cfg*."""
    text = cfg.server.instructions_file.read_text(encoding="utf-8")
    return _PLACEHOLDER.sub(lambda m: str(lookup(cfg, m.group(1))), text)


def main(argv: list[str] | None = None) -> int:
    """``python -m senior_dev_code.config get <dotted.key> [--config <toml>]``.

    How the PowerShell scripts read a value instead of repeating it.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="senior_dev_code.config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="print one resolved value, e.g. server.port or paths.db_path")
    g.add_argument("key")
    g.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    value = lookup(load_config(args.config), args.key)
    print(value.as_posix() if isinstance(value, Path) else value)
    return 0


def _build_opts(raw: dict[str, Any], cfg_dir: Path, index_dir: Path) -> BuildOpts:
    """[build] with [build.properties] merged over the defaults, not replacing them.

    target_framework_root always comes out absolute, so one config.toml works on
    every machine: empty means the build step's own folder under index_dir.
    """
    raw = dict(raw)
    props = dict(BuildOpts().properties)
    props.update({k: str(v) for k, v in raw.pop("properties", {}).items()})
    tfr = str(raw.pop("target_framework_root", "") or "").strip()
    root = Path(tfr) if tfr else index_dir / "build" / "refasm"
    if not root.is_absolute():
        root = cfg_dir / root
    return BuildOpts(properties=props, target_framework_root=root.resolve().as_posix(), **raw)


if __name__ == "__main__":
    raise SystemExit(main())
