"""File walk, exclusion rules, generated-code detection and content hashing.

The exclusion rules are exact and ordered; every skipped file is
recorded with the reason so ``index_status`` can report it.  A file the advisor
cannot see must never be silently absent -- that is how a grounded agent starts
guessing.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from .config import Config

# --- 5.1 A: directory names excluded at any depth --------------------------

EXCLUDED_DIRS: frozenset[str] = frozenset(
    n.lower()
    for n in (
        ".git",
        ".vs",
        ".svn",
        "obj",
        "bin",
        "node_modules",
        "packages",
        "TestResults",
        "PrecompiledWeb",
        "_ReSharper.Caches",
        ".nuget",
        "BuildProcessTemplates",
        "App_Data",
        "Zotero",
    )
)

# --- 5.1 B: directory-name suffix ------------------------------------------

RELEASE_MIRROR_SUFFIX = "-release"

# --- 5.1 C: indexed extensions ---------------------------------------------

INDEXED_EXTS: frozenset[str] = frozenset(
    (".cs", ".js", ".aspx", ".ascx", ".master", ".ashx", ".asmx", ".cshtml")
)

Lang = Literal["csharp", "javascript", "markup", "razor", "unknown"]

LANG_BY_EXT: dict[str, Lang] = {
    ".cs": "csharp",
    ".js": "javascript",
    ".aspx": "markup",
    ".ascx": "markup",
    ".master": "markup",
    ".ashx": "markup",
    ".asmx": "markup",
    ".cshtml": "razor",
}

MARKUP_EXTS: frozenset[str] = frozenset((".aspx", ".ascx", ".master"))

# --- 5.1 D: vendor JavaScript ----------------------------------------------

VENDOR_JS = re.compile(
    r"(\.min\.js$)|(^jquery)|(^bootstrap)|(^ag-grid)|(^daypilot)|(^ckeditor)"
    r"|(^moment)|(^pdf\.)|(^xlsx)|(^telerik)|(^angular)|(^react)|(^vue)"
    r"|(^lodash)|(^underscore)|(^d3[.-])|(^chart)|(^hammer)|(^cropper)"
    r"|(^dropzone)|(^autonumeric)|(^imask)|(^html2canvas)|(^viewer\.js$)",
    re.IGNORECASE,
)

# --- 5.2 generated code -----------------------------------------------------

GENERATED_BASENAME = re.compile(
    r"(\.designer\.cs|\.g\.cs|\.g\.i\.cs|^Reference\.cs|AssemblyInfo\.cs)$",
    re.IGNORECASE,
)
GENERATED_MARKER = re.compile(rb"auto-?generated", re.IGNORECASE)


# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    """One row destined for the ``files`` table."""

    path: Path
    rel_path: str
    side: str
    ext: str
    lang: Lang
    size_bytes: int
    mtime_ns: int
    content_hash: str
    line_count: int
    is_generated: bool
    indexed: bool
    skip_reason: str | None

    @property
    def abs_posix(self) -> str:
        return self.path.as_posix()


def is_excluded_dir(name: str) -> str | None:
    """Directory-level exclusion (rules A and B).  Returns a reason or None."""
    low = name.lower()
    if low in EXCLUDED_DIRS:
        return "excluded_dir"
    if low.endswith(RELEASE_MIRROR_SUFFIX):
        return "release_mirror"
    return None


def is_vendor_js(path: Path) -> bool:
    return path.suffix.lower() == ".js" and bool(VENDOR_JS.search(path.name))


def content_hash(p: Path) -> str:
    """BLAKE2b-128 over the raw bytes."""
    h = hashlib.blake2b(digest_size=16)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(data)
    return h.hexdigest()


def count_lines(data: bytes) -> int:
    if not data:
        return 0
    n = data.count(b"\n")
    return n if data.endswith(b"\n") else n + 1


def detect_generated(path: Path, head: bytes) -> bool:
    """Basename pattern, or an auto-generated marker in the
    first five non-empty lines."""
    if GENERATED_BASENAME.search(path.name):
        return True
    seen = 0
    for raw in head.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        seen += 1
        if GENERATED_MARKER.search(line):
            return True
        if seen >= 5:
            break
    return False


def walk_files(cfg: Config) -> Iterator[Path]:
    """Yield every candidate file under ``paths.workspaces``.

    Directories matching rules A/B are pruned, so their contents are never
    stat'ed at all.  Read-only: ``os.walk`` opens nothing.
    """
    root = cfg.paths.workspaces
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if is_excluded_dir(d) is None]
        dirnames.sort()
        base = Path(dirpath)
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() in INDEXED_EXTS:
                yield base / fn


def describe(path: Path, cfg: Config, *, stat_result: os.stat_result | None = None) -> FileRecord:
    """Build a :class:`FileRecord`, applying rules D and E.

    Reads the file once (bytes) unless it is over the size gate, in which case
    only the stat is used and the hash is computed by streaming.
    """
    st = stat_result or path.stat()
    ext = path.suffix.lower()
    lang: Lang = LANG_BY_EXT.get(ext, "unknown")
    side = cfg.side_of(path)

    skip_reason: str | None = None
    if is_vendor_js(path):
        skip_reason = "vendor_js"
    elif st.st_size > cfg.index.max_file_bytes:
        skip_reason = "too_large"

    if skip_reason is not None:
        # Recorded but not parsed.  Hash by streaming; do not hold it in memory.
        return FileRecord(
            path=path,
            rel_path=cfg.rel(path),
            side=side,
            ext=ext,
            lang=lang,
            size_bytes=st.st_size,
            mtime_ns=st.st_mtime_ns,
            content_hash=content_hash(path),
            line_count=0,
            is_generated=bool(GENERATED_BASENAME.search(path.name)),
            indexed=False,
            skip_reason=skip_reason,
        )

    data = path.read_bytes()
    return FileRecord(
        path=path,
        rel_path=cfg.rel(path),
        side=side,
        ext=ext,
        lang=lang,
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
        content_hash=hash_bytes(data),
        line_count=count_lines(data),
        is_generated=detect_generated(path, data[:4096]) if lang == "csharp" else False,
        indexed=True,
        skip_reason=None,
    )


def read_source(path: Path) -> bytes:
    """Read a source file as raw bytes.

    Offsets and hashes are always computed against these bytes; decoding
    happens only when a string is stored.  Read-only.
    """
    return path.read_bytes()


def decode(data: bytes) -> str:
    """Decode source bytes for display.  Handles the UTF-8 BOM that every
    ``App/`` file carries."""
    return data.decode("utf-8-sig", errors="replace")
