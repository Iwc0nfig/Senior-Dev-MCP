// SqlParser: parses every module definition with Microsoft's own T-SQL parser
// (ScriptDom) and emits, per module, one JSON line with:
//   aliases   alias -> table/view/cte/temp/derived/tvf per query scope
//   joins     column = column equalities in ON / WHERE clauses (ISNULL/COALESCE unwrapped)
//   outputs   select-list items of result / cte / derived / temp / insert sources,
//             each with the base (target, column, kind) refs its expression reads
//   colrefs   every alias-resolved column reference with its clause
//   literals  column-vs-literal comparisons (=, <>, IN, BETWEEN, simple CASE)
//   calls     EXEC targets and user function calls
//   writes    statements that change data or schema: [kind, target, detail, line]
//
// Usage: SqlParser <modules.jsonl> <catalog_columns.json> <out.jsonl>
//   modules.jsonl        {"obj_id":1,"definition":"..."} per line
//   catalog_columns.json {"tablename_lower": ["col1", ...], ...}  (for unqualified refs)
//
// Lines are 1-based within the stored definition (LF-normalised).

using System.Text;
using System.Text.Json;
using Microsoft.SqlServer.TransactSql.ScriptDom;

namespace SeniorDevDb.SqlParser;

public static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length is < 3 or > 4 || (args.Length == 4 && args[3] != "--dynamic"))
        {
            Console.Error.WriteLine("usage: SqlParser <modules.jsonl> <catalog_columns.json> <out.jsonl> [--dynamic]");
            return 2;
        }
        bool dynamicSql = args.Length == 4;
        var catalog = JsonSerializer.Deserialize<Dictionary<string, string[]>>(File.ReadAllText(args[1]))!
            .ToDictionary(kv => kv.Key.ToLowerInvariant(),
                          kv => new HashSet<string>(kv.Value, StringComparer.OrdinalIgnoreCase));
        var opts = new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
        int n = 0, failed = 0, dyn = 0, dynParsed = 0;
        using var outp = new StreamWriter(args[2], false, new UTF8Encoding(false));
        foreach (var line in File.ReadLines(args[0]))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            using var doc = JsonDocument.Parse(line);
            long objId = doc.RootElement.GetProperty("obj_id").GetInt64();
            string def = doc.RootElement.GetProperty("definition").GetString() ?? "";
            var result = ParseOne(objId, def, catalog, dynamicSql);
            if (!result.Ok) failed++;
            if (result.dynamic.Count > 0) dyn++;
            if (result.dynamic.Any(d => (bool)d[3]!)) dynParsed++;
            outp.WriteLine(JsonSerializer.Serialize(result, opts));
            n++;
        }
        Console.Error.WriteLine($"parsed {n} modules, {failed} with errors"
                                + (dynamicSql ? $"; dynamic SQL in {dyn}, outlined in {dynParsed}" : ""));
        return 0;
    }

    static ModuleResult ParseOne(long objId, string def, Dictionary<string, HashSet<string>> catalog,
                                 bool dynamicSql = false)
    {
        var parser = new TSql160Parser(initialQuotedIdentifiers: true);
        TSqlFragment? frag;
        IList<ParseError> errors;
        using (var rdr = new StringReader(def))
            frag = parser.Parse(rdr, out errors);
        var res = new ModuleResult { obj_id = objId, Ok = errors.Count == 0 };
        foreach (var e in errors.Take(5))
            res.errors.Add($"line {e.Line}: {e.Message}");
        if (frag == null) return res;
        try
        {
            var v = new Walker(res, catalog);
            frag.Accept(v);
        }
        catch (Exception ex)
        {
            res.Ok = false;
            res.errors.Add("walker: " + ex.GetType().Name + ": " + ex.Message);
        }
        if (dynamicSql)
        {
            try
            {
                DynamicSql.Outline(frag, res, catalog);
            }
            catch (Exception ex)
            {
                // the static parse stands; only the dynamic outline is lost
                res.dynamic.Add(new object?[] { "?", 0, 0, false, false, 0 });
                Console.Error.WriteLine($"obj {objId}: dynamic SQL outline failed: {ex.GetType().Name}: {ex.Message}");
            }
        }
        return res;
    }
}

/// <summary>
/// Dynamic SQL (a string built into a variable, then run by sp_executesql or EXEC(@v)) is invisible
/// to the parse above.  This pass rebuilds the variable's text from its string literals, in source
/// order, up to the first piece that is not a literal (a parameter, a CASE, a function call), and
/// parses that prefix as SQL.  Report procedures build "select ... from ... joins ... where " from
/// literals and only the WHERE from parameters, so the prefix carries their outputs, aliases and
/// joins.  Each literal is placed on its own source line, so line numbers match the module.
/// </summary>
static class DynamicSql
{
    const int StmtBase = 100000;     // dynamic statements are numbered from here (static ones from 1)

    // a non-literal piece (parameter, CASE, function call) becomes this variable: valid T-SQL wherever
    // the piece stands for a value, a table name or a bracketed identifier, and never mistaken for a literal
    const string Placeholder = "@__dyn";

    sealed record Piece(string Text, int Line, bool Literal);

    sealed class Collector : TSqlFragmentVisitor
    {
        public readonly Dictionary<string, List<Piece>> Pieces = new(StringComparer.OrdinalIgnoreCase);
        public readonly List<string> Executed = new();

        public override void ExplicitVisit(SetVariableStatement node)
        {
            Assign(node.Variable.Name, node.Expression, node.AssignmentKind);
            base.ExplicitVisit(node);
        }

        public override void ExplicitVisit(DeclareVariableStatement node)
        {
            foreach (var d in node.Declarations)
                if (d.Value != null) Assign(d.VariableName.Value, d.Value, AssignmentKind.Equals);
            base.ExplicitVisit(node);
        }

        public override void ExplicitVisit(SelectSetVariable node)
        {
            Assign(node.Variable.Name, node.Expression, node.AssignmentKind);
            base.ExplicitVisit(node);
        }

        public override void ExplicitVisit(ExecuteStatement node)
        {
            switch (node.ExecuteSpecification?.ExecutableEntity)
            {
                case ExecutableProcedureReference p:
                    var name = p.ProcedureReference?.ProcedureReference?.Name?.BaseIdentifier?.Value;
                    if (name != null && (name.Equals("sp_executesql", StringComparison.OrdinalIgnoreCase)
                                         || name.Equals("sp_sqlexec", StringComparison.OrdinalIgnoreCase))
                        && p.Parameters.Count > 0 && p.Parameters[0].ParameterValue is VariableReference v)
                        Executed.Add(v.Name);
                    break;
                case ExecutableStringList s:
                    foreach (var part in s.Strings)
                        if (part is VariableReference sv) Executed.Add(sv.Name);
                    break;
            }
            base.ExplicitVisit(node);
        }

        void Assign(string var, ScalarExpression? expr, AssignmentKind kind)
        {
            if (expr == null) return;
            var items = new List<ScalarExpression>();
            Flatten(expr, items);
            int start = 0;
            if (kind == AssignmentKind.Equals)
            {
                if (items.Count > 0 && items[0] is VariableReference self &&
                    self.Name.Equals(var, StringComparison.OrdinalIgnoreCase))
                    start = 1;                         // set @v = @v + '...' appends
                else
                    Pieces[var] = new List<Piece>();
            }
            else if (kind != AssignmentKind.AddEquals)
                return;
            if (!Pieces.TryGetValue(var, out var list)) Pieces[var] = list = new List<Piece>();
            for (int i = start; i < items.Count; i++)
                list.Add(items[i] is StringLiteral lit ? new Piece(lit.Value, lit.StartLine, true)
                                                       : new Piece(Placeholder, items[i].StartLine, false));
        }

        static void Flatten(ScalarExpression e, List<ScalarExpression> into)
        {
            switch (e)
            {
                case BinaryExpression b when b.BinaryExpressionType == BinaryExpressionType.Add:
                    Flatten(b.FirstExpression, into);
                    Flatten(b.SecondExpression, into);
                    break;
                case ParenthesisExpression p:
                    Flatten(p.Expression, into);
                    break;
                default:
                    into.Add(e);
                    break;
            }
        }
    }

    public static void Outline(TSqlFragment frag, ModuleResult res, Dictionary<string, HashSet<string>> catalog)
    {
        var c = new Collector();
        frag.Accept(c);
        int k = 0;
        foreach (var var in c.Executed.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (!c.Pieces.TryGetValue(var, out var pieces) || !pieces.Any(p => p.Literal))
            {
                res.dynamic.Add(new object?[] { var, 0, 0, false, false, 0 });
                continue;
            }
            k++;
            var full = Build(pieces);                                    // every piece, placeholders included
            int firstOther = pieces.FindIndex(p => !p.Literal);
            var prefix = firstOther < 0 ? full : Build(pieces.Take(firstOther));   // literals up to the first other
            bool parsed = false;
            foreach (var candidate in Candidates(full, prefix))
            {
                var parser = new TSql160Parser(initialQuotedIdentifiers: true);
                TSqlFragment? sub;
                IList<ParseError> errors;
                using (var rdr = new StringReader(candidate))
                    sub = parser.Parse(rdr, out errors);
                if (errors.Count > 0 || sub is not TSqlScript script || !script.Batches.SelectMany(b => b.Statements).Any())
                    continue;
                sub.Accept(new Walker(res, catalog, "dynamic:" + var, StmtBase * k));
                parsed = true;
                break;
            }
            res.dynamic.Add(new object?[] { var, pieces[0].Line, pieces[^1].Line, parsed, firstOther < 0,
                                            full.Trim().Length });
        }
    }

    /// Each piece on its own source line, so the parse of the text reports the module's line numbers.
    static string Build(IEnumerable<Piece> pieces)
    {
        var text = new StringBuilder();
        int line = 1;
        foreach (var p in pieces)
        {
            if (p.Line > line) { text.Append('\n', p.Line - line); line = p.Line; }
            text.Append(p.Text);
            line += p.Text.Count(ch => ch == '\n');
        }
        return text.ToString();
    }

    static readonly System.Text.RegularExpressions.Regex Tail = new(
        @"(?is)(\b(where|and|or|on|not|like|in|between|having|set|values|order\s+by|group\s+by)|[,=+(<>'!])\s*$");

    static string TrimTail(string s)
    {
        for (int i = 0; i < 20; i++)
        {
            var t = Tail.Replace(s.TrimEnd(), "", 1).TrimEnd();
            if (t.Length == s.TrimEnd().Length) break;
            s = t;
        }
        return s;
    }

    /// The whole text with placeholders; then the literal prefix, as built and cut before its last
    /// WHERE / GROUP BY / ORDER BY (the clauses most often finished by non-literal pieces).
    static IEnumerable<string> Candidates(string full, string prefix)
    {
        yield return TrimTail(full);
        if (prefix != full) yield return TrimTail(prefix);
        foreach (var kw in new[] { @"\bwhere\b", @"\bgroup\s+by\b", @"\border\s+by\b" })
        {
            var ms = System.Text.RegularExpressions.Regex.Matches(prefix, kw,
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            if (ms.Count > 0) yield return TrimTail(prefix[..ms[^1].Index]);
        }
    }
}

public sealed class ModuleResult
{
    public long obj_id { get; set; }
    public bool Ok { get; set; }
    public List<string> errors { get; set; } = new();
    public List<object[]> aliases { get; set; } = new();   // [stmt, alias, target, kind, line]
    public List<object?[]> joins { get; set; } = new();    // [stmt, type, lt, lc, rt, rc, predicate, line]
    public List<object?[]> outputs { get; set; } = new();  // [stmt, owner, ordinal, name, expr, sources[[t,c,kind]], line]
    public List<object?[]> colrefs { get; set; } = new();  // [t, c, usage, line]
    public List<object?[]> literals { get; set; } = new(); // [t, c, op, value, line]
    public List<object[]> calls { get; set; } = new();     // [callee, kind, line]
    public List<object?[]> writes { get; set; } = new();   // [kind, target, detail, line]
    public List<object?[]> dynamic { get; set; } = new();  // [var, first_line, last_line, parsed, complete, chars]
}

sealed class Target
{
    public string Name = "";
    public string Kind = "object";     // object | cte | temp | derived | tvf | variable
}

sealed class Walker : TSqlFragmentVisitor
{
    readonly ModuleResult _r;
    readonly Dictionary<string, HashSet<string>> _catalog;
    internal readonly List<Dictionary<string, Target>> _scopes = new();
    readonly Dictionary<QuerySpecification, string> _owners = new();
    readonly Stack<string> _usage = new();
    readonly HashSet<string> _ctes = new(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<(string, string, string, int)> _seenRefs = new();
    int _stmt;
    // owner of top-level SELECT outputs: "result", or "dynamic:@var" for the re-parsed text of dynamic SQL
    readonly string _resultOwner;

    public Walker(ModuleResult r, Dictionary<string, HashSet<string>> catalog, string resultOwner = "result",
                  int stmtStart = 0)
    {
        _r = r;
        _catalog = catalog;
        _resultOwner = resultOwner;
        _stmt = stmtStart;
    }

    // ---- statements ---------------------------------------------------------

    public override void Visit(TSqlStatement node)
    {
        if (node is BeginEndBlockStatement or IfStatement or WhileStatement or TryCatchStatement
            or CreateProcedureStatement or AlterProcedureStatement or CreateOrAlterProcedureStatement
            or CreateViewStatement or AlterViewStatement or CreateOrAlterViewStatement
            or CreateFunctionStatement or AlterFunctionStatement or CreateOrAlterFunctionStatement
            or CreateTriggerStatement or AlterTriggerStatement or CreateOrAlterTriggerStatement)
            return;
        _stmt++;
    }

    public override void ExplicitVisit(SelectStatement node)
    {
        string owner = _resultOwner;
        if (node.Into != null)
        {
            var n = node.Into.BaseIdentifier.Value;
            owner = (n.StartsWith("#") ? "temp:" : "into:") + n;
        }
        if (node.WithCtesAndXmlNamespaces != null)
            foreach (var cte in node.WithCtesAndXmlNamespaces.CommonTableExpressions)
                _ctes.Add(cte.ExpressionName.Value);
        bool assigns = FirstSpec(node.QueryExpression)?.SelectElements.Any(e => e is SelectSetVariable) ?? false;
        if (!assigns)
            foreach (var qs in TopSpecs(node.QueryExpression))
                _owners[qs] = owner;
        base.ExplicitVisit(node);
    }

    public override void ExplicitVisit(CommonTableExpression node)
    {
        _ctes.Add(node.ExpressionName.Value);
        var specs = TopSpecs(node.QueryExpression).ToList();
        foreach (var qs in specs)
            _owners[qs] = "cte:" + node.ExpressionName.Value;
        base.ExplicitVisit(node);
        if (node.Columns.Count > 0)
        {
            // explicit column list renames the outputs positionally
            var owner = "cte:" + node.ExpressionName.Value;
            int i = 0;
            foreach (var row in _r.outputs.Where(o => (string?)o[1] == owner))
            {
                if (i < node.Columns.Count) row[3] = node.Columns[i].Value;
                i++;
            }
        }
    }

    public override void ExplicitVisit(QueryDerivedTable node)
    {
        if (node.Alias != null)
            foreach (var qs in TopSpecs(node.QueryExpression))
                _owners[qs] = "derived:" + node.Alias.Value;
        base.ExplicitVisit(node);
    }

    public override void ExplicitVisit(InsertStatement node)
    {
        var spec = node.InsertSpecification;
        string? target = TargetName(spec.Target);
        if (target != null)
        {
            if (spec.InsertSource is SelectInsertSource sis)
                foreach (var qs in TopSpecs(sis.Select))
                    _owners[qs] = (target.StartsWith("#") || target.StartsWith("@") ? "temp:" : "insert:") + target;
            foreach (var c in spec.Columns)
            {
                var col = c.MultiPartIdentifier?.Identifiers.LastOrDefault()?.Value;
                if (col != null) AddRef(target, col, "insert", c.StartLine, TargetKind(target));
            }
        }
        base.ExplicitVisit(node);
        if (target != null && spec.Columns.Count > 0 && spec.InsertSource is SelectInsertSource)
        {
            var owner = (target.StartsWith("#") || target.StartsWith("@") ? "temp:" : "insert:") + target;
            int i = 0;
            foreach (var row in _r.outputs.Where(o => (string?)o[1] == owner && (int)o[0]! == _stmt))
            {
                if (i < spec.Columns.Count)
                    row[3] = spec.Columns[i].MultiPartIdentifier.Identifiers.Last().Value;
                i++;
            }
        }
    }

    public override void ExplicitVisit(UpdateSpecification node)
    {
        var scope = new Dictionary<string, Target>(StringComparer.OrdinalIgnoreCase);
        if (node.FromClause != null)
            foreach (var tr in node.FromClause.TableReferences) Collect(tr, scope);
        var tname = TargetName(node.Target);
        if (tname != null && !scope.ContainsKey(tname))
            scope[tname] = new Target { Name = tname, Kind = TargetKind(tname) };
        _scopes.Add(scope);
        AddWrite("update", tname, scope, node.WhereClause, node.FromClause, node.TopRowFilter, node.StartLine);
        foreach (var sc in node.SetClauses)
        {
            if (sc is AssignmentSetClause asc && asc.Column != null)
            {
                var (t, c, k) = Resolve(asc.Column);
                if (t == null && tname != null)
                {
                    var tt = scope.TryGetValue(tname, out var x) ? x : null;
                    t = tt?.Name ?? tname; k = tt?.Kind ?? "object";
                    c = asc.Column.MultiPartIdentifier.Identifiers.Last().Value;
                }
                if (t != null && c != null) AddRef(t, c, "set", asc.Column.StartLine, k);
            }
        }
        base.ExplicitVisit(node);
        _scopes.RemoveAt(_scopes.Count - 1);
    }

    public override void ExplicitVisit(DeleteSpecification node)
    {
        var scope = new Dictionary<string, Target>(StringComparer.OrdinalIgnoreCase);
        if (node.FromClause != null)
            foreach (var tr in node.FromClause.TableReferences) Collect(tr, scope);
        var tname = TargetName(node.Target);
        if (tname != null && !scope.ContainsKey(tname))
            scope[tname] = new Target { Name = tname, Kind = TargetKind(tname) };
        _scopes.Add(scope);
        AddWrite("delete", tname, scope, node.WhereClause, node.FromClause, node.TopRowFilter, node.StartLine);
        base.ExplicitVisit(node);
        _scopes.RemoveAt(_scopes.Count - 1);
    }

    // ---- statements that change data or schema ------------------------------

    void AddWrite(string kind, string? tname, Dictionary<string, Target> scope, WhereClause? where,
                  FromClause? from, TopRowFilter? top, int line)
    {
        if (tname == null) return;
        var real = scope.TryGetValue(tname, out var t) ? t.Name : tname;
        var filters = new List<string>();
        if (where != null) filters.Add(where.Cursor != null ? "current of cursor" : "where");
        if (from != null && from.TableReferences.Any(tr => tr is QualifiedJoin qj && qj.QualifiedJoinType == QualifiedJoinType.Inner))
            filters.Add("inner join");
        if (top != null) filters.Add("top");
        _r.writes.Add(new object?[] { kind, real, filters.Count == 0 ? "no filter" : string.Join("+", filters), line });
    }

    public override void Visit(InsertStatement node)
    {
        var t = TargetName(node.InsertSpecification.Target);
        if (t != null) _r.writes.Add(new object?[] { "insert", t, null, node.StartLine });
    }

    public override void Visit(MergeStatement node)
    {
        var t = TargetName(node.MergeSpecification.Target);
        if (t != null) _r.writes.Add(new object?[] { "merge", t, null, node.StartLine });
    }

    public override void Visit(TruncateTableStatement node) =>
        _r.writes.Add(new object?[] { "truncate", node.TableName.BaseIdentifier.Value, null, node.StartLine });

    public override void Visit(DropObjectsStatement node)
    {
        var kind = node switch
        {
            DropTableStatement => "drop table", DropViewStatement => "drop view",
            DropProcedureStatement => "drop procedure", DropFunctionStatement => "drop function",
            DropTriggerStatement => "drop trigger", DropSynonymStatement => "drop synonym", _ => "drop",
        };
        foreach (var o in node.Objects)
            _r.writes.Add(new object?[] { kind, o.BaseIdentifier.Value, node.IsIfExists ? "if exists" : null, node.StartLine });
    }

    public override void Visit(AlterTableStatement node)
    {
        string? detail = node switch
        {
            AlterTableAddTableElementStatement a => "add " + string.Join(", ",
                a.Definition.ColumnDefinitions.Select(c => "column " + c.ColumnIdentifier.Value)
                 .Concat(a.Definition.TableConstraints.Select(c => "constraint " + (c.ConstraintIdentifier?.Value ?? "")))),
            AlterTableDropTableElementStatement d => "drop " + string.Join(", ",
                d.AlterTableDropTableElements.Select(e => (e.TableElementType == TableElementType.Column ? "column " :
                    e.TableElementType == TableElementType.Constraint ? "constraint " : "") + e.Name.Value)),
            AlterTableAlterColumnStatement c => "alter column " + c.ColumnIdentifier.Value,
            _ => node.GetType().Name.Replace("AlterTable", "").Replace("Statement", "").ToLowerInvariant(),
        };
        _r.writes.Add(new object?[] { "alter table", node.SchemaObjectName.BaseIdentifier.Value, detail, node.StartLine });
    }

    public override void Visit(CreateTableStatement node)
    {
        var n = node.SchemaObjectName.BaseIdentifier.Value;
        if (!n.StartsWith("#")) _r.writes.Add(new object?[] { "create table", n, null, node.StartLine });
    }

    public override void Visit(CreateIndexStatement node) =>
        _r.writes.Add(new object?[] { "create index", node.OnName.BaseIdentifier.Value, node.Name.Value, node.StartLine });

    public override void Visit(DropIndexStatement node)
    {
        foreach (var c in node.DropIndexClauses.OfType<DropIndexClause>())
            _r.writes.Add(new object?[] { "drop index", c.Object.BaseIdentifier.Value, c.Index.Value, node.StartLine });
    }

    public override void Visit(ProcedureStatementBody node) => AddModule(node, node.ProcedureReference.Name);
    public override void Visit(ViewStatementBody node) => AddModule(node, node.SchemaObjectName);
    public override void Visit(FunctionStatementBody node) => AddModule(node, node.Name);
    public override void Visit(TriggerStatementBody node) => AddModule(node, node.Name);

    void AddModule(TSqlStatement node, SchemaObjectName name)
    {
        var verb = node.GetType().Name;                       // e.g. CreateOrAlterProcedureStatement
        var what = verb.Contains("Procedure") ? "procedure" : verb.Contains("View") ? "view"
                 : verb.Contains("Function") ? "function" : "trigger";
        var how = verb.StartsWith("CreateOrAlter") ? "create or alter" : verb.StartsWith("Create") ? "create" : "alter";
        _r.writes.Add(new object?[] { how + " " + what, name.BaseIdentifier.Value, null, node.StartLine });
    }

    // ---- query specification: scope + outputs -------------------------------

    public override void ExplicitVisit(QuerySpecification node)
    {
        var scope = new Dictionary<string, Target>(StringComparer.OrdinalIgnoreCase);
        if (node.FromClause != null)
            foreach (var tr in node.FromClause.TableReferences) Collect(tr, scope);
        _scopes.Add(scope);
        if (_owners.TryGetValue(node, out var owner))
        {
            int ord = 0;
            foreach (var el in node.SelectElements)
            {
                ord++;
                if (el is SelectScalarExpression sse)
                {
                    string? name = sse.ColumnName?.Value;
                    if (name == null && sse.Expression is ColumnReferenceExpression cre)
                        name = cre.MultiPartIdentifier?.Identifiers.LastOrDefault()?.Value;
                    var sources = SourcesOf(sse.Expression);
                    _r.outputs.Add(new object?[] { _stmt, owner, ord, name, Text(sse.Expression, 400), sources, sse.StartLine });
                }
                else if (el is SelectStarExpression star)
                {
                    var q = star.Qualifier?.Identifiers.LastOrDefault()?.Value;
                    var srcs = new List<object?[]>();
                    if (q != null && LookupAlias(q) is Target t)
                        srcs.Add(new object?[] { t.Name, "*", t.Kind });
                    else
                        foreach (var t2 in scope.Values) srcs.Add(new object?[] { t2.Name, "*", t2.Kind });
                    _r.outputs.Add(new object?[] { _stmt, owner, ord, "*", Text(star, 200), srcs, star.StartLine });
                }
            }
        }
        base.ExplicitVisit(node);
        _scopes.RemoveAt(_scopes.Count - 1);
    }

    // ---- clause usage labels ------------------------------------------------

    void WithUsage(string u, Action a) { _usage.Push(u); try { a(); } finally { _usage.Pop(); } }

    public override void ExplicitVisit(SelectScalarExpression node) => WithUsage("select", () => base.ExplicitVisit(node));
    public override void ExplicitVisit(WhereClause node) => WithUsage("where", () => base.ExplicitVisit(node));
    public override void ExplicitVisit(GroupByClause node) => WithUsage("group", () => base.ExplicitVisit(node));
    public override void ExplicitVisit(OrderByClause node) => WithUsage("order", () => base.ExplicitVisit(node));
    public override void ExplicitVisit(HavingClause node) => WithUsage("where", () => base.ExplicitVisit(node));

    public override void ExplicitVisit(QualifiedJoin node)
    {
        string jt = node.QualifiedJoinType switch
        {
            QualifiedJoinType.Inner => "inner",
            QualifiedJoinType.LeftOuter => "left",
            QualifiedJoinType.RightOuter => "right",
            QualifiedJoinType.FullOuter => "full",
            _ => "join",
        };
        if (node.SearchCondition != null)
            RecordEqualities(node.SearchCondition, jt, Text(node.SearchCondition, 300));
        node.FirstTableReference?.Accept(this);
        node.SecondTableReference?.Accept(this);
        if (node.SearchCondition != null)
            WithUsage("join", () => node.SearchCondition.Accept(this));
    }

    public override void Visit(WhereClause node)
    {
        if (node.SearchCondition != null)
            RecordEqualities(node.SearchCondition, "where", null);
    }

    // ---- references, literals, calls ---------------------------------------

    public override void Visit(ColumnReferenceExpression node)
    {
        if (node.ColumnType != ColumnType.Regular || node.MultiPartIdentifier == null) return;
        var (t, c, k) = Resolve(node);
        if (t != null && c != null)
            AddRef(t, c, _usage.Count > 0 ? _usage.Peek() : "other", node.StartLine, k);
    }

    public override void Visit(BooleanComparisonExpression node)
    {
        var op = node.ComparisonType switch
        {
            BooleanComparisonType.Equals => "=",
            BooleanComparisonType.NotEqualToBrackets or BooleanComparisonType.NotEqualToExclamation => "<>",
            BooleanComparisonType.GreaterThan => ">",
            BooleanComparisonType.LessThan => "<",
            BooleanComparisonType.GreaterThanOrEqualTo => ">=",
            BooleanComparisonType.LessThanOrEqualTo => "<=",
            _ => "?",
        };
        LiteralPair(node.FirstExpression, node.SecondExpression, op, node.StartLine);
        LiteralPair(node.SecondExpression, node.FirstExpression, op, node.StartLine);
    }

    public override void Visit(InPredicate node)
    {
        if (Unwrap(node.Expression) is ColumnReferenceExpression cre && node.Values.Count > 0)
        {
            var vals = node.Values.Select(Lit).ToList();
            if (vals.All(v => v != null))
            {
                var (t, c, _) = Resolve(cre);
                if (c != null) _r.literals.Add(new object?[] { t, c, node.NotDefined ? "not in" : "in", string.Join(",", vals), node.StartLine });
            }
        }
    }

    public override void Visit(BooleanTernaryExpression node)
    {
        if (Unwrap(node.FirstExpression) is ColumnReferenceExpression cre)
        {
            var a = Lit(node.SecondExpression); var b = Lit(node.ThirdExpression);
            if (a != null && b != null)
            {
                var (t, c, _) = Resolve(cre);
                if (c != null) _r.literals.Add(new object?[] { t, c, "between", a + ".." + b, node.StartLine });
            }
        }
    }

    public override void Visit(SimpleCaseExpression node)
    {
        if (Unwrap(node.InputExpression) is ColumnReferenceExpression cre)
        {
            var (t, c, _) = Resolve(cre);
            if (c == null) return;
            foreach (var w in node.WhenClauses)
            {
                var v = Lit(w.WhenExpression);
                if (v != null) _r.literals.Add(new object?[] { t, c, "case", v, w.StartLine });
            }
        }
    }

    // DATEADD(day, ...) etc.: ScriptDom parses the datepart as a column reference
    internal static readonly HashSet<string> DateFns = new(StringComparer.OrdinalIgnoreCase)
        { "DATEADD", "DATEDIFF", "DATEDIFF_BIG", "DATEPART", "DATENAME", "DATETRUNC", "DATE_BUCKET" };

    public override void ExplicitVisit(FunctionCall node)
    {
        if (DateFns.Contains(node.FunctionName.Value) && node.Parameters.Count > 1)
        {
            for (int i = 1; i < node.Parameters.Count; i++) node.Parameters[i].Accept(this);
            node.OverClause?.Accept(this);
            return;
        }
        base.ExplicitVisit(node);
    }

    public override void Visit(ExecutableProcedureReference node)
    {
        var name = node.ProcedureReference?.ProcedureReference?.Name?.BaseIdentifier?.Value;
        if (name != null) _r.calls.Add(new object[] { name, "exec", node.StartLine });
    }

    public override void Visit(FunctionCall node)
    {
        // user functions are schema-qualified in T-SQL (dbo.fn_x)
        if (node.CallTarget is MultiPartIdentifierCallTarget mp && mp.MultiPartIdentifier.Count >= 1)
            _r.calls.Add(new object[] { node.FunctionName.Value, "function", node.StartLine });
    }

    // ---- helpers ------------------------------------------------------------

    internal void Collect(TableReference tr, Dictionary<string, Target> scope, bool record = true)
    {
        switch (tr)
        {
            case NamedTableReference ntr:
            {
                var name = ntr.SchemaObject.BaseIdentifier.Value;
                var t = new Target { Name = name, Kind = TargetKind(name) };
                var alias = ntr.Alias?.Value ?? name;
                scope[alias] = t;
                if (record) _r.aliases.Add(new object[] { _stmt, alias, name, t.Kind, ntr.StartLine });
                break;
            }
            case QueryDerivedTable qdt when qdt.Alias != null:
            {
                var t = new Target { Name = qdt.Alias.Value, Kind = "derived" };
                scope[qdt.Alias.Value] = t;
                if (record) _r.aliases.Add(new object[] { _stmt, qdt.Alias.Value, qdt.Alias.Value, "derived", qdt.StartLine });
                break;
            }
            case SchemaObjectFunctionTableReference sf:
            {
                var name = sf.SchemaObject.BaseIdentifier.Value;
                var alias = sf.Alias?.Value ?? name;
                scope[alias] = new Target { Name = name, Kind = "tvf" };
                if (record) _r.aliases.Add(new object[] { _stmt, alias, name, "tvf", sf.StartLine });
                if (record) _r.calls.Add(new object[] { name, "function", sf.StartLine });
                break;
            }
            case VariableTableReference vt:
            {
                var name = vt.Variable.Name;
                var alias = vt.Alias?.Value ?? name;
                scope[alias] = new Target { Name = name, Kind = "temp" };
                if (record) _r.aliases.Add(new object[] { _stmt, alias, name, "temp", vt.StartLine });
                break;
            }
            case JoinTableReference jtr:
                Collect(jtr.FirstTableReference, scope, record);
                Collect(jtr.SecondTableReference, scope, record);
                break;
            case JoinParenthesisTableReference jp:
                Collect(jp.Join, scope, record);
                break;
        }
    }

    string TargetKind(string name) =>
        name.StartsWith("#") || name.StartsWith("@") ? "temp" : _ctes.Contains(name) ? "cte" : "object";

    static string? TargetName(TableReference? tr) => tr switch
    {
        NamedTableReference ntr => ntr.SchemaObject.BaseIdentifier.Value,
        VariableTableReference vt => vt.Variable.Name,
        _ => null,
    };

    Target? LookupAlias(string alias)
    {
        for (int i = _scopes.Count - 1; i >= 0; i--)
            if (_scopes[i].TryGetValue(alias, out var t)) return t;
        return null;
    }

    internal (string? t, string? c, string k) Resolve(ColumnReferenceExpression cre)
    {
        var ids = cre.MultiPartIdentifier?.Identifiers;
        if (ids == null || ids.Count == 0) return (null, null, "");
        var col = ids[^1].Value;
        if (ids.Count >= 2)
        {
            var q = ids[^2].Value;
            var t = LookupAlias(q);
            if (t != null) return (t.Name, col, t.Kind);
            return (q, col, TargetKind(q));      // qualified by a bare table name not in scope
        }
        // unqualified: innermost scope first, use the catalog to disambiguate
        for (int i = _scopes.Count - 1; i >= 0; i--)
        {
            var scope = _scopes[i];
            if (scope.Count == 0) continue;
            if (scope.Count == 1)
            {
                var only = scope.Values.First();
                return (only.Name, col, only.Kind);
            }
            var hits = scope.Values
                .Where(t => t.Kind == "object" && _catalog.TryGetValue(t.Name.ToLowerInvariant(), out var cols) && cols.Contains(col))
                .Select(t => t.Name).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            if (hits.Count == 1) return (hits[0], col, "object");
            if (hits.Count > 1) return (null, col, "");
        }
        return (null, col, "");
    }

    void AddRef(string t, string c, string usage, int line, string kind)
    {
        if (kind != "object") return;   // usage index is about real tables/views
        if (_seenRefs.Add((t.ToLowerInvariant(), c.ToLowerInvariant(), usage, line)))
            _r.colrefs.Add(new object?[] { t, c, usage, line });
    }

    List<object?[]> SourcesOf(ScalarExpression expr)
    {
        var finder = new ColumnFinder(this);
        expr.Accept(finder);
        return finder.Found;
    }

    // Resolves column refs of one expression while walking it, pushing the FROM
    // scope of any subquery it contains so inner aliases resolve to inner tables.
    sealed class ColumnFinder : TSqlFragmentVisitor
    {
        readonly Walker _w;
        readonly HashSet<string> _seen = new(StringComparer.OrdinalIgnoreCase);
        public readonly List<object?[]> Found = new();
        public ColumnFinder(Walker w) { _w = w; }

        public override void ExplicitVisit(QuerySpecification node)
        {
            var scope = new Dictionary<string, Target>(StringComparer.OrdinalIgnoreCase);
            if (node.FromClause != null)
                foreach (var tr in node.FromClause.TableReferences) _w.Collect(tr, scope, record: false);
            _w._scopes.Add(scope);
            try { base.ExplicitVisit(node); }
            finally { _w._scopes.RemoveAt(_w._scopes.Count - 1); }
        }

        public override void ExplicitVisit(FunctionCall node)
        {
            if (DateFns.Contains(node.FunctionName.Value) && node.Parameters.Count > 1)
            {
                for (int i = 1; i < node.Parameters.Count; i++) node.Parameters[i].Accept(this);
                node.OverClause?.Accept(this);
                return;
            }
            base.ExplicitVisit(node);
        }

        public override void Visit(ColumnReferenceExpression node)
        {
            if (node.ColumnType != ColumnType.Regular || node.MultiPartIdentifier == null) return;
            var (t, c, k) = _w.Resolve(node);
            if (c == null) return;
            if (_seen.Add((t ?? "?") + "." + c)) Found.Add(new object?[] { t, c, k });
        }
    }

    void RecordEqualities(BooleanExpression cond, string type, string? predicate)
    {
        var eqs = new EqualityFinder();
        cond.Accept(eqs);
        foreach (var eq in eqs.Found)
        {
            var left = ColumnsIn(eq.FirstExpression);
            var right = ColumnsIn(eq.SecondExpression);
            if (left.Count == 0 || right.Count == 0) continue;
            foreach (var l in left)
                foreach (var r in right)
                {
                    var (lt, lc, lk) = Resolve(l);
                    var (rt, rc, rk) = Resolve(r);
                    if (lt == null || rt == null || lc == null || rc == null) continue;
                    if (string.Equals(lt, rt, StringComparison.OrdinalIgnoreCase) && lk == rk) continue;
                    _r.joins.Add(new object?[] { _stmt, type, lt, lc, rt, rc, predicate ?? Text(eq, 300), eq.StartLine });
                }
        }
    }

    // Column refs that an equality side is "about": the column itself, or the
    // arguments of ISNULL/COALESCE/CAST/CONVERT wrapped around it.
    static List<ColumnReferenceExpression> ColumnsIn(ScalarExpression e)
    {
        var u = Unwrap(e);
        var list = new List<ColumnReferenceExpression>();
        switch (u)
        {
            case ColumnReferenceExpression c when c.ColumnType == ColumnType.Regular:
                list.Add(c); break;
            case FunctionCall fc when fc.FunctionName.Value.Equals("isnull", StringComparison.OrdinalIgnoreCase):
                foreach (var p in fc.Parameters) list.AddRange(ColumnsIn(p));
                break;
            case CoalesceExpression co:
                foreach (var p in co.Expressions) list.AddRange(ColumnsIn(p));
                break;
            case CastCall cc:
                list.AddRange(ColumnsIn(cc.Parameter)); break;
            case ConvertCall cv:
                list.AddRange(ColumnsIn(cv.Parameter)); break;
        }
        return list;
    }

    static ScalarExpression Unwrap(ScalarExpression e)
    {
        while (e is ParenthesisExpression pe) e = pe.Expression;
        return e;
    }

    void LiteralPair(ScalarExpression colSide, ScalarExpression litSide, string op, int line)
    {
        if (Unwrap(colSide) is not ColumnReferenceExpression cre) return;
        var v = Lit(litSide);
        if (v == null) return;
        var (t, c, _) = Resolve(cre);
        if (c != null) _r.literals.Add(new object?[] { t, c, op, v, line });
    }

    static string? Lit(ScalarExpression? e)
    {
        if (e == null) return null;
        e = Unwrap(e);
        return e switch
        {
            IntegerLiteral i => i.Value,
            NumericLiteral n => n.Value,
            StringLiteral s => "'" + s.Value + "'",
            UnaryExpression { UnaryExpressionType: UnaryExpressionType.Negative, Expression: IntegerLiteral i2 } => "-" + i2.Value,
            _ => null,
        };
    }

    static QuerySpecification? FirstSpec(QueryExpression? q) => q switch
    {
        QuerySpecification qs => qs,
        BinaryQueryExpression b => FirstSpec(b.FirstQueryExpression),
        QueryParenthesisExpression p => FirstSpec(p.QueryExpression),
        _ => null,
    };

    static IEnumerable<QuerySpecification> TopSpecs(QueryExpression? q)
    {
        // Only the first branch of a UNION names the columns; later branches would
        // duplicate ordinals, so lineage is taken from the first branch only.
        var f = FirstSpec(q);
        if (f != null) yield return f;
    }

    static string Text(TSqlFragment f, int max)
    {
        if (f.ScriptTokenStream == null || f.FirstTokenIndex < 0) return "";
        var sb = new StringBuilder();
        bool ws = false;
        for (int i = f.FirstTokenIndex; i <= f.LastTokenIndex && sb.Length < max + 20; i++)
        {
            var tok = f.ScriptTokenStream[i];
            if (tok.TokenType is TSqlTokenType.WhiteSpace or TSqlTokenType.SingleLineComment or TSqlTokenType.MultilineComment)
            {
                ws = true;
                continue;
            }
            if (ws && sb.Length > 0) sb.Append(' ');
            ws = false;
            sb.Append(tok.Text);
        }
        var s = sb.ToString();
        return s.Length > max ? s[..max] + "…" : s;
    }
}

sealed class EqualityFinder : TSqlFragmentVisitor
{
    public readonly List<BooleanComparisonExpression> Found = new();
    public override void Visit(BooleanComparisonExpression node)
    {
        if (node.ComparisonType == BooleanComparisonType.Equals) Found.Add(node);
    }
    // do not descend into subqueries: their equalities have their own scope
    public override void ExplicitVisit(ScalarSubquery node) { }
    public override void ExplicitVisit(ExistsPredicate node) { }
    public override void ExplicitVisit(InPredicate node) { if (node.Subquery == null) base.ExplicitVisit(node); }
}
