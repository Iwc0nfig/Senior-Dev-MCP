"""Shared plumbing for the tool layer.

Two things here matter more than the rest:

* :func:`translate_location` maps a hit inside a synthesised designer partial
  back to the markup line that produced it.  Those files exist only in
  ``.index/`` -- telling a user to open one would be telling them to open a file
  that does not exist.
* :func:`scope_of` builds the ``searched_scope`` string.  When a tool finds
  nothing, that string is the difference between a grounded "not found" and an
  invitation to guess.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from .. import db as dbm
from ..models import Loc, SymbolRef
from ..roslyn.client import RoslynPool, RoslynError, RoslynUnavailable

KNOWN_LIMITATIONS = [
    "Source control is TFVC, not git: no commit history, blame, or diff is available to "
    "this tool. Any statement about when or why code changed is outside what it can verify.",
    "App/ control fields are synthesised from markup; ASP.NET generates the real ones at "
    "runtime. They are marked is_synthetic.",
    "Controls inside data-bound templates (ItemTemplate etc.) get a synthesised field here, "
    "but ASP.NET does not generate one at runtime. They are marked "
    "in_data_bound_template.",
    "tree-sitter-c-sharp cannot use 'async', 'var' or 'await' as identifiers in every "
    "position C# permits; affected declarations may be missing from the graph layer "
    "(Roslyn is unaffected).",
    "HTML, CSS, SQL and Delphi (.pas) files are searchable via search_text but have no "
    "symbol graph.",
    "Tags and directives inside <%-- server comments --%> are excluded: they are "
    "commented out and do not run. 426 prefixed tags in App/ fall in this category.",
]

# Used only when no compiler index has been built (index_status replaces it
# with the measured coverage otherwise).
LIVE_ROSLYN_ONLY_LIMITATION = (
    "No compiler index has been built (scripts/build-workspace.py, then scripts/reindex.py). "
    "The live Roslyn hosts cover only the projects listed in the two .sln files: 8 of the "
    "~290 .csproj under Legacy are in Legacy.sln; code in the others is visible to the "
    "graph and to search_text, but not to a compiler-exact layer."
)

NOT_INDEXED = [
    "*.html, *.css, *.htm (searchable via search_text, no symbol graph)",
    "*.config, *.sql, *.pas (Delphi)",
    "-Release mirror folders (App-Release, Modern-Release, Legacy.Entities-Release)",
    "vendor JavaScript (402 files, recorded with indexed=0)",
    "files over 2 MB (4 files, recorded with indexed=0)",
    "obj/, bin/, packages/, node_modules/, PrecompiledWeb/, App_Data/",
]


@dataclass
class Ctx:
    cfg: Config
    pool: RoslynPool
    _local: threading.local = field(default_factory=threading.local, repr=False)

    @classmethod
    def create(cls, cfg: Config) -> "Ctx":
        if not cfg.paths.db_path.is_file():
            raise FileNotFoundError(
                f"{cfg.paths.db_path} not found. Run: python scripts/reindex.py"
            )
        return cls(cfg=cfg, pool=RoslynPool(cfg))

    @property
    def con(self) -> sqlite3.Connection:
        """A read-only connection owned by the calling thread.

        The MCP server runs synchronous tool functions on a worker thread pool
        (``anyio.to_thread.run_sync``) and a sqlite3 connection may only be used from
        its creating thread.  One per thread keeps that legal without relaxing
        ``check_same_thread``; read-only connections cannot contend.
        """
        con = getattr(self._local, "con", None)
        if con is None:
            con = dbm.connect(self.cfg.paths.db_path, read_only=True, timeouts=self.cfg.timeouts)
            self._local.con = con
        return con

    # -- scope strings ----------------------------------------------------

    def graph_scope(self) -> str:
        m = dbm.all_meta(self.con)
        return (
            f"structural graph index: {m.get('files_indexed', '?')} files / "
            f"{m.get('lines_indexed', '?')} lines across Legacy (net48) and "
            f"Modern (net8), built {m.get('built_at', 'unknown')}"
        )

    def roslyn_scope(self, side: str) -> str:
        host = self.pool.get(side)
        st = host.state
        sln = Path(st.solution).name if st.solution else (
            self.cfg.paths.net48_solution.name if side == "net48"
            else self.cfg.paths.net8_solution.name
        )
        extra = ""
        if side == "net48":
            extra = f" + App.Website synthetic project ({st.website_documents} documents)"
        return (
            f"Roslyn {side}: {sln} ({st.projects_loaded}/{st.projects_total} projects "
            f"loaded, {st.failure_count} workspace failures){extra}"
        )

    # -- file helpers ------------------------------------------------------

    def rel(self, path: str | Path) -> str:
        return self.cfg.rel(path)

    def file_row(self, path: str) -> Optional[sqlite3.Row]:
        """The ``files`` row for an absolute or workspace-relative path.

        The index is keyed on the workspace-relative path, so absolute paths are reduced
        to that first.  No case-insensitive fallback, deliberately: on a case-sensitive
        filesystem ``lower(rel_path)=lower(?)`` can match a *different* file, and
        answering confidently about the wrong file is what this design exists to prevent.
        """
        p = str(path).replace("\\", "/")
        rel = self.cfg.rel(p) if Path(p).is_absolute() else p.lstrip("/")
        return self.con.execute(
            "SELECT * FROM files WHERE rel_path=?", (rel,)
        ).fetchone()

    def resolve_path(self, given: str) -> Optional[Path]:
        """Accept absolute, workspace-relative, or App-relative paths."""
        g = str(given).replace("\\", "/").strip()
        cands = [Path(g)]
        if not Path(g).is_absolute():
            cands += [
                self.cfg.paths.workspaces / g,
                self.cfg.paths.website_root / g,
            ]
        for c in cands:
            if c.is_file():
                return c
        row = self.con.execute(
            "SELECT rel_path FROM files WHERE rel_path=? OR rel_path LIKE ?",
            (g, f"%/{g.lstrip('/')}"),
        ).fetchone()
        if row:
            p = Path(self.cfg.abs_of(row["rel_path"]))
            if p.is_file():
                return p
        return None


# --- synthetic designer translation ----------------------------------------


SYNTH_SUFFIX = ".synth.designer.cs"


def is_synthetic_path(cfg: Config, path: str) -> bool:
    """Whether *path* names a synthesised designer partial.

    Accepts either the absolute path (what a Roslyn location carries) or the bare
    basename the index stores in ``pages.synth_path``.
    """
    p = str(path).replace("\\", "/")
    if p.startswith(cfg.paths.designer_dir.as_posix()):
        return True
    return "/" not in p and p.endswith(SYNTH_SUFFIX)


def synth_name(path: str) -> str:
    """The stored ``pages.synth_path`` key for a synthetic file path."""
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def translate_location(ctx: Ctx, file: str, line: int, snippet: str = "") -> tuple[Loc, bool]:
    """Return a citable location, plus whether it came from a synthetic file.

    A hit inside ``.index/designer/*.synth.designer.cs`` is rewritten to the
    markup tag that produced the field, because the synthetic file does not
    exist on disk and must never be handed to a user as somewhere to look.
    """
    fpath = str(file).replace("\\", "/")
    if not is_synthetic_path(ctx.cfg, fpath):
        return (
            Loc(
                file=fpath,
                rel_path=ctx.rel(fpath),
                line_start=line,
                line_end=line,
                snippet=snippet,
            ),
            False,
        )

    row = ctx.con.execute(
        "SELECT f.rel_path FROM pages p JOIN files f ON f.file_id=p.markup_file "
        "WHERE p.synth_path=?",
        (synth_name(fpath),),
    ).fetchone()
    if row is None:
        return (
            Loc(
                file=fpath,
                rel_path="(synthesised, not on disk)",
                line_start=line,
                line_end=line,
                snippet=snippet,
            ),
            True,
        )

    ctl_line = _synth_line_to_markup_line(ctx, fpath, snippet)
    return (
        Loc(
            file=ctx.cfg.abs_of(row["rel_path"]),
            rel_path=row["rel_path"],
            line_start=ctl_line or 1,
            line_end=ctl_line or 1,
            snippet=snippet,
        ),
        True,
    )


def _synth_line_to_markup_line(ctx: Ctx, synth_path: str, snippet: str) -> Optional[int]:
    """The emitted field carries its markup line in the doc comment; fall back to
    a lookup by field name."""
    name = snippet.strip().rstrip(";").split(" ")[-1] if snippet else ""
    if not name:
        return None
    row = ctx.con.execute(
        "SELECT c.line FROM controls c JOIN pages p ON p.markup_file=c.markup_file "
        "WHERE p.synth_path=? AND c.ctl_id=? ORDER BY c.line LIMIT 1",
        (synth_name(synth_path), name.lstrip("@")),
    ).fetchone()
    return row["line"] if row else None


# --- reading source ---------------------------------------------------------


@lru_cache(maxsize=256)
def _read_lines(path: str) -> list[str]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    return data.decode("utf-8-sig", errors="replace").splitlines()


def snippet_for(path: str, line: int) -> str:
    lines = _read_lines(path)
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:400]
    return ""


# --- symbol rows ------------------------------------------------------------


def symbol_from_row(ctx: Ctx, row: sqlite3.Row, *, with_snippet: bool = True) -> SymbolRef:
    path = ctx.cfg.abs_of(row["rel_path"])
    synthetic = bool(row["is_synthetic"])
    snippet = row["signature"] or ""
    if with_snippet and not synthetic:
        snippet = snippet_for(path, row["line_start"]) or snippet
    return SymbolRef(
        name=row["name"],
        kind=row["kind"],
        fqn=row["fqn"],
        container=row["container"],
        signature=row["signature"],
        side=row["side"],
        loc=Loc(
            file=path,
            rel_path=row["rel_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            col_start=row["col_start"],
            snippet=snippet,
        ),
        is_generated=bool(row["is_generated"]),
        is_synthetic=synthetic,
    )


SYMBOL_SELECT = (
    "SELECT s.*, f.rel_path FROM symbols s JOIN files f ON f.file_id = s.file_id"
)


# --- Roslyn access ----------------------------------------------------------


def try_roslyn(ctx: Ctx, side: str, method: str, params: dict[str, Any]) -> tuple[Any, Optional[str]]:
    """Call a host.  Returns ``(result, unavailable_reason)``.

    Never raises for an unavailable host: the caller turns the reason into
    ``status: 'unavailable'`` with no results, rather than silently falling back to
    the graph and passing off a weaker answer as a compiler answer.
    """
    host = ctx.pool.get(side)
    try:
        if host.proc is None or host.proc.poll() is not None:
            if not host.available():
                # Not configured, or never built: a stated absence, not a crash.
                return None, host.unavailable_reason()
            return None, (
                f"Roslyn {side} host is not running. Call index_status for details."
            )
        return host.call(method, params), None
    except RoslynUnavailable as exc:
        return None, str(exc)
    except RoslynError as exc:
        if exc.code in ("not_loaded", "timeout"):
            return None, str(exc)
        raise
