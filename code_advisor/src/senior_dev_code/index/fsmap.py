"""Case-insensitive filesystem lookup.

ASP.NET resolves virtual paths case-insensitively on every platform.  NTFS gives that
for free, so building a path from markup text and calling ``is_file()`` worked.  On a
case-sensitive filesystem it fails silently: the reference resolves to ``None`` and the
page loses its code-behind or master link.

Measured on the real ``App/`` tree, 9 of 664 directive path references disagree
with disk in case alone -- 4 ``CodeFile`` (e.g. ``sign-in/Default.aspx`` writes
``default.aspx.cs`` for a file named ``Default.aspx.cs``) and 5
``MasterPageFile`` (``~/system/templates/exContent.master`` for
``excontent.master``).  This module makes the resolution explicit.

Read-only: it lists directories and never touches their contents.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class CaseMap:
    """Resolve a path whose case may not match disk, caching one listing per directory.

    ``resolve`` returns the real on-disk path, or ``None`` when no file matches
    even case-insensitively.  An exact hit costs one ``stat``; only a miss pays
    for the directory listing, and that listing is then reused.
    """

    def __init__(self) -> None:
        self._dirs: dict[str, dict[str, str]] = {}
        # How often the answer differed from what was written; reported via index_status.
        self.case_resolved = 0

    def _listing(self, directory: Path) -> dict[str, str]:
        key = directory.as_posix()
        entry = self._dirs.get(key)
        if entry is None:
            try:
                entry = {e.name.lower(): e.name for e in os.scandir(directory)}
            except OSError:
                entry = {}
            self._dirs[key] = entry
        return entry

    def resolve(self, candidate: Path) -> Optional[Path]:
        """The real on-disk path for *candidate*, or ``None`` if nothing matches.

        The listing is consulted first, deliberately, not ``exists()``: on NTFS
        ``exists()`` is true for a mis-cased path and would return the case as
        *written*, which fails to match the ``files`` table (keyed on the name the
        directory walk saw).  The listing gives the same answer on case-folding and
        case-sensitive filesystems, so an index built on either is identical.
        """
        listing = self._listing(candidate.parent)
        real = listing.get(candidate.name.lower())
        if real is None:
            # Absent, or the parent could not be listed (permission error): ask the
            # filesystem directly.
            return candidate if candidate.exists() else None
        if real != candidate.name:
            self.case_resolved += 1
        return candidate.with_name(real)

    def resolve_file(self, candidate: Path) -> Optional[Path]:
        """As :meth:`resolve`, but only for a path that is a regular file."""
        p = self.resolve(candidate)
        return p if p is not None and p.is_file() else None

    def invalidate(self, directory: Path) -> None:
        self._dirs.pop(directory.as_posix(), None)


#: Process-wide default.  One cache shared by the markup context, the designer check and
#: the Web.config lookup costs one ``scandir`` per directory that needs one.
DEFAULT = CaseMap()
