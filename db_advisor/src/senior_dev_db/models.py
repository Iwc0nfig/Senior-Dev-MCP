"""Shared output shapes (the Code Advisor's envelope, with DB layers).

Every tool returns a ToolEnvelope subclass.  ``layer`` says how much to trust an
answer; ``searched_scope`` turns "nothing found" into a grounded statement;
``caveats`` carry conflicts and limits that the agent must surface verbatim.
Field names are short on purpose: these payloads are read by an agent, and every
byte is tokens.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# catalog   = SQL Server's own metadata (exact, as of the last prepare)
# sql-parse = Microsoft ScriptDom parse of module text (exact syntax; alias-resolved)
# code      = Roslyn syntax extraction from the C# workspace (exact syntax)
# data      = values observed in the tables at prepare time
# curated   = model-derived reading of code/SQL, every claim citation-verified
# live      = asked the database just now (validate_sql only)
Layer = Literal["catalog", "sql-parse", "code", "data", "curated", "live", "mixed", "text", "sandbox"]
# error       = the request itself is invalid (bad SQL, refused statement, bad regex): fix it and call again
# unavailable = a layer could not answer (database or sandbox unreachable, not configured, busy)
Status = Literal["ok", "no_results", "partial", "unavailable", "error"]


class ToolEnvelope(BaseModel):
    status: Status
    layer: Layer = Field(description="Which layer(s) produced this; see server instructions.")
    searched_scope: str = Field(description="Exactly what was searched. Quote it when reporting 'not found'.")
    truncated: bool = False
    total_available: Optional[int] = None
    caveats: list[str] = Field(default_factory=list,
                               description="Conflicts and limitations for THIS result. Surface verbatim.")


class Hit(BaseModel):
    kind: str
    name: str = Field(description="Canonical name: pass it to the other tools.")
    match: str = Field(description="The text that matched.")
    detail: Optional[str] = None


class FindOut(ToolEnvelope):
    hits: list[Hit] = []


class Step(BaseModel):
    step: str
    detail: str
    cite: Optional[str] = Field(default=None, description="path:line or Object:line")


class TraceOut(ToolEnvelope):
    target: Optional[str] = None
    chain: list[Step] = []
    sql_objects: list[str] = Field(default=[], description="SQL objects that produce this screen's data, outermost first")
    base_tables: list[str] = []
    params: list[dict[str, Any]] = []
    companion_code: list[Step] = Field(default=[], description="C# registrations to edit when the data shape changes")
    sides: list[str] = Field(default=[], description="every code side / catalogue: registered here, not registered, "
                                                     "or why the side has none")
    columns: list[str] = Field(default=[], description="per registration side: every registered column as DataField@line")


class ColRow(BaseModel):
    name: str
    type: str
    null: bool = True
    meaning: Optional[str] = None
    values: Optional[str] = Field(default=None, description="value=label list, compact")
    link: Optional[str] = Field(default=None, description="FK or implicit relationship: Table.Column (confidence)")
    note: Optional[str] = None


class TableOut(ToolEnvelope):
    name: Optional[str] = None
    kind: Optional[str] = None
    rows: Optional[int] = None
    description: Optional[str] = None
    pk: Optional[str] = None
    row_kinds: list[str] = Field(default=[], description="rows are of several kinds: filter on this column when counting")
    columns: list[ColRow] = []
    referenced_by: list[str] = []
    entity: list[str] = []


class ValueSource(BaseModel):
    source: str
    via: str
    confidence: str
    values: dict[str, str]
    cite: Optional[str] = None


class ColumnOut(ToolEnvelope):
    table: Optional[str] = None
    column: Optional[str] = None
    type: Optional[str] = None
    nullable: Optional[bool] = None
    default: Optional[str] = None
    description: Optional[str] = None
    meaning: list[str] = Field(default=[], description="curated / derived meanings with citations")
    values: dict[str, str] = Field(default={}, description="value -> label variants from all linked sources (' | ')")
    value_sources: list[str] = Field(default=[], description="each source: kind name [side] (how linked, confidence) cite")
    observed: Optional[str] = Field(default=None, description="value (rows) observed in the data")
    notes: list[str] = []
    relationships: list[str] = []
    code: list[str] = Field(default=[], description="entity reads/writes: path:line  property as Type")
    exposed_as: list[str] = Field(default=[], description="views / grid / report columns showing it")


class ValueMapOut(ToolEnvelope):
    query: str = ""
    maps: list[ValueSource] = []
    used_by: list[str] = []


class DefinitionOut(ToolEnvelope):
    name: Optional[str] = None
    kind: Optional[str] = None
    modified: Optional[str] = None
    line_start: int = 1
    line_end: int = 0
    total_lines: int = 0
    text: str = Field(default="", description="Verbatim module text, each line prefixed with its number.")


class OutlineOut(ToolEnvelope):
    name: Optional[str] = None
    kind: Optional[str] = None
    purpose: Optional[str] = None
    params: list[str] = []
    aliases: list[str] = Field(default=[], description="alias=Target @line")
    joins: list[str] = []
    outputs: list[str] = Field(default=[], description="ordinal name = expression <- base columns @line")
    filters: list[str] = Field(default=[], description="column op literal (decoded) @line")
    calls: list[str] = []
    called_by: list[str] = []
    temp_tables: list[str] = []


class UsageOut(ToolEnvelope):
    table: Optional[str] = None
    column: Optional[str] = None
    sql: list[str] = Field(default=[], description="Object:line usage")
    code: list[str] = Field(default=[], description="path:line container")
    ui: list[str] = []


class PathOut(ToolEnvelope):
    paths: list[list[str]] = Field(default=[], description="each path: hop strings 'A.col -> B.col (kind, confidence) e.g. <predicate> [Obj:line]'")


class ImpactOut(ToolEnvelope):
    target: Optional[str] = None
    db_dependents: list[str] = []
    ui_consumers: list[str] = []
    code_consumers: list[str] = []
    companion_edits: list[str] = Field(default=[], description="what else must change, with insertion anchors")


class StyleOut(ToolEnvelope):
    kind: str = ""
    team_rules: list[str] = Field(default=[], description="Written by the DB developers. Authoritative: follow these.")
    team_example: Optional[str] = None
    rules: list[str] = Field(default=[], description="Measured in existing code; older code may differ. Team rules win.")
    idioms: list[str] = []
    exemplar: Optional[str] = None
    skeleton: Optional[str] = None


class SearchSqlOut(ToolEnvelope):
    matches: list[str] = Field(default=[], description="Object:line: text")


class DraftOut(ToolEnvelope):
    draft_id: Optional[str] = Field(default=None, description="Pass to validate_sql / test_sql as draft_id.")
    name: Optional[str] = None
    kind: Optional[str] = None
    live_modified: Optional[str] = Field(default=None, description="modify_date of the live text the draft is based on")
    changed_lines: list[int] = Field(default=[], description="lines of the script you added or changed")
    diff: str = Field(default="", description="unified diff: live text -> script")
    style: list[str] = Field(default=[], description="team-style notes on the changed lines only")
    script: str = Field(default="", description="the full script: the live text with the edits applied, verbatim")


class ValidateOut(ToolEnvelope):
    ok: bool = False
    errors: list[str] = []
    changes: list[str] = Field(default=[], description="what each data/schema-changing statement touches")
    style: list[str] = Field(default=[], description="Deviations from the team SQL style (warnings; ok is not affected)")
    result_columns: list[str] = []


class PreviewOut(ToolEnvelope):
    columns: list[str] = Field(default=[], description="name type")
    rows: list[list[Optional[str]]] = Field(default=[], description="first rows only (text cut to 100 chars)")
    more_rows: bool = False
    elapsed_ms: Optional[int] = None


class StatusOut(ToolEnvelope):
    built_at: Optional[str] = None
    changed_since_build: Optional[str] = Field(default=None, description="objects changed in the live DB after the build")
    database: Optional[str] = None
    counts: dict[str, int] = {}
    phases: dict[str, str] = {}
    known_limitations: list[str] = []
    masked: list[str] = []
    sandbox: Optional[str] = Field(default=None, description="the clone test_sql runs scripts in")


class SandboxResult(BaseModel):
    batch: int
    columns: list[str] = []
    rows: list[list[Optional[str]]] = Field(default=[], description="first rows only (text cut to 100 chars)")
    more_rows: bool = False
    row_count: int = Field(default=0, description="rows returned (counted up to the cap; more_rows says there are more)")
    total_rows: Optional[int] = Field(default=None, description="rows the result set had in all (counted up to 100,000)")
    matched_rows: Optional[int] = Field(default=None, description="with find_rows: rows of the set that match it")


class SandboxOut(ToolEnvelope):
    ok: bool = False
    errors: list[str] = Field(default=[], description="run-time errors per batch (FK, duplicate key, conversion ...)")
    events: list[str] = Field(default=[], description="in order, like SSMS messages: rows affected, result sets, PRINT/RAISERROR")
    result_sets: list[SandboxResult] = []
    reset: bool = Field(default=False, description="the clone was restored from its snapshot after this run")
    elapsed_ms: Optional[int] = None
