// CodeExtractor: syntax-level extraction of everything in the C# workspace that
// explains the database.  One JSON object per line, field "t" names the record:
//
//   enum        C# enum: members with values (implicit values computed per C# rules)
//   value_enum  class X : ValueEnum<X, T[, E]> with static readonly X Field = new X("Label", value)
//   const       const field (container path, name, value expression)
//   memtype     MemTypeCatalog.Register / MemTypeCatalogue.RegisterMemType(new MemType("Name").AddItem("Label", value)...)
//   entity      class with  TableName = "SomeTable"  (Initialize())
//   colread     <reader>.GetXxx("Column") with the property it is assigned to and any
//               wrapper type (T.resolve(...), (T)cast) / declared member type
//   fields      string[] _Fields = { "Col", ... } in an entity (columns it writes)
//   view        new MasterView { ... Columns = { new Column {...} } }
//   report      new Report { ... Filters = {...}, Sections = { Columns = {...} } }
//   sql         SQL text in a string literal / concatenation / interpolation ({} = hole)
//
// Usage: CodeExtractor <workspaces_root> <out.jsonl> <subdir> [<subdir>...]
// Paths in the output are relative to workspaces_root with forward slashes.

using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace SeniorDevDb.CodeExtractor;

public static class Program
{
    static readonly HashSet<string> ExcludedDirs = new(StringComparer.OrdinalIgnoreCase)
    {
        "bin", "obj", "packages", "node_modules", ".vs", ".git", "testresults", "precompiledweb",
        "app_data", "buildprocesstemplates",
    };

    public static int Main(string[] args)
    {
        if (args.Length < 3)
        {
            Console.Error.WriteLine("usage: CodeExtractor <workspaces_root> <out.jsonl> <subdir> [<subdir>...]");
            return 2;
        }
        var root = Path.GetFullPath(args[0]);
        var files = new List<string>();
        foreach (var sub in args.Skip(2))
            Walk(Path.Combine(root, sub), files);
        Console.Error.WriteLine($"{files.Count} .cs files");

        var bag = new ConcurrentBag<string>();
        var opts = new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
        int errors = 0;
        var parseOpts = new CSharpParseOptions(LanguageVersion.Preview, DocumentationMode.None);
        Parallel.ForEach(files, new ParallelOptions { MaxDegreeOfParallelism = Environment.ProcessorCount }, f =>
        {
            try
            {
                var text = File.ReadAllText(f);
                var rel = Path.GetRelativePath(root, f).Replace('\\', '/');
                var tree = CSharpSyntaxTree.ParseText(text, parseOpts, path: rel);
                var x = new Extractor(rel, tree);
                foreach (var rec in x.Run())
                    bag.Add(JsonSerializer.Serialize(rec, opts));
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref errors);
                Console.Error.WriteLine($"{f}: {ex.GetType().Name}: {ex.Message}");
            }
        });
        using var w = new StreamWriter(args[1], false, new UTF8Encoding(false));
        foreach (var line in bag.OrderBy(s => s, StringComparer.Ordinal)) w.WriteLine(line);
        Console.Error.WriteLine($"wrote {bag.Count} records, {errors} file errors");
        return 0;
    }

    static void Walk(string dir, List<string> files)
    {
        if (!Directory.Exists(dir)) return;
        foreach (var d in Directory.EnumerateDirectories(dir))
        {
            var name = Path.GetFileName(d);
            if (ExcludedDirs.Contains(name) || name.EndsWith("-Release", StringComparison.OrdinalIgnoreCase)) continue;
            var attr = File.GetAttributes(d);
            if ((attr & FileAttributes.ReparsePoint) != 0) continue;
            Walk(d, files);
        }
        foreach (var f in Directory.EnumerateFiles(dir, "*.cs"))
            if (!f.EndsWith(".designer.cs", StringComparison.OrdinalIgnoreCase) || f.Contains("Resource", StringComparison.OrdinalIgnoreCase) == false)
                files.Add(f);
    }
}

sealed class Extractor
{
    readonly string _rel;
    readonly SyntaxTree _tree;
    readonly List<Dictionary<string, object?>> _out = new();

    static readonly Regex SqlLike = new(
        @"\b(select\b[\s\S]*\bfrom|insert\s+into|update\s+[\w\[\]\.#@]+\s+set|delete\s+from|exec(ute)?\s+[\w\[\]\.]+|merge\s+into|with\s*\(\s*nolock\s*\))",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    static readonly Regex BareProc = new(@"^\s*(?:\[?dbo\]?\.)?\[?(usp_\w+)\]?\s*$", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    static readonly HashSet<string> ReaderMethods = new(StringComparer.Ordinal)
    {
        "GetByte", "GetString", "GetInt16", "GetInt32", "GetInt64", "GetDecimal", "GetBoolean", "GetDate",
        "GetDateTime", "GetGuid", "GetDouble", "GetChar", "GetFloat", "GetValue", "GetNullableInt32",
        "GetNullableDecimal", "GetNullableDateTime", "GetNullableByte", "GetNullableBoolean", "GetBytes",
        "GetSafeString", "GetInt", "GetDecimalOrZero", "GetNullableDate", "GetTimeSpan",
        "GetEnum", "GetValueEnum", "GetNullableEnum", "Field",
    };
    static readonly HashSet<string> Primitive = new(StringComparer.Ordinal)
    {
        "byte", "byte?", "int", "int?", "short", "short?", "long", "long?", "string", "bool", "bool?", "decimal",
        "decimal?", "DateTime", "DateTime?", "double", "char", "Guid", "object",
    };

    public Extractor(string rel, SyntaxTree tree)
    {
        _rel = rel;
        _tree = tree;
    }

    int Line(SyntaxNode n) => n.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
    int LineEnd(SyntaxNode n) => n.GetLocation().GetLineSpan().EndLinePosition.Line + 1;

    Dictionary<string, object?> Rec(string t, SyntaxNode at)
    {
        var d = new Dictionary<string, object?> { ["t"] = t, ["rel"] = _rel, ["line"] = Line(at) };
        _out.Add(d);
        return d;
    }

    static string Fqn(SyntaxNode n)
    {
        var parts = new List<string>();
        for (var p = n; p != null; p = p.Parent)
        {
            switch (p)
            {
                case BaseTypeDeclarationSyntax t: parts.Add(t.Identifier.Text); break;
                case BaseNamespaceDeclarationSyntax ns: parts.Add(ns.Name.ToString()); break;
            }
        }
        parts.Reverse();
        return string.Join(".", parts);
    }

    static string? StringValue(ExpressionSyntax? e)
    {
        if (e is LiteralExpressionSyntax l && l.IsKind(SyntaxKind.StringLiteralExpression)) return l.Token.ValueText;
        return null;
    }

    static string Short(string s, int max)
    {
        s = Regex.Replace(s, @"\s+", " ").Trim();
        return s.Length > max ? s[..max] + "…" : s;
    }

    public IEnumerable<Dictionary<string, object?>> Run()
    {
        var root = _tree.GetRoot();
        foreach (var node in root.DescendantNodes())
        {
            switch (node)
            {
                case EnumDeclarationSyntax e: Enum(e); break;
                case ClassDeclarationSyntax c: Class(c); break;
                case FieldDeclarationSyntax f when f.Modifiers.Any(SyntaxKind.ConstKeyword): Const(f); break;
                case ObjectCreationExpressionSyntax oc: Creation(oc); break;
                case InvocationExpressionSyntax inv: Invocation(inv); break;
            }
        }
        Sql(root);
        return _out;
    }

    // ---- enums / value enums / consts -----------------------------------------

    void Enum(EnumDeclarationSyntax e)
    {
        var d = Rec("enum", e);
        d["name"] = e.Identifier.Text;
        d["fqn"] = Fqn(e);
        d["underlying"] = e.BaseList?.Types.FirstOrDefault()?.ToString();
        var members = new List<object?[]>();
        long? next = 0;
        foreach (var m in e.Members)
        {
            string? value;
            string? expr = m.EqualsValue?.Value.ToString();
            if (expr != null)
            {
                value = TryInt(m.EqualsValue!.Value, out var v) ? v.ToString() : null;
                next = value != null ? long.Parse(value) + 1 : null;
            }
            else
            {
                value = next?.ToString();
                next = next.HasValue ? next + 1 : null;
            }
            members.Add(new object?[] { m.Identifier.Text, value, expr, Line(m) });
        }
        d["members"] = members;
    }

    static bool TryInt(ExpressionSyntax e, out long v)
    {
        v = 0;
        switch (e)
        {
            case LiteralExpressionSyntax l when l.Token.Value is IConvertible c && l.IsKind(SyntaxKind.NumericLiteralExpression):
                try { v = Convert.ToInt64(c); return true; } catch { return false; }
            case LiteralExpressionSyntax l when l.IsKind(SyntaxKind.CharacterLiteralExpression):
                v = (char)l.Token.Value!; return true;
            case PrefixUnaryExpressionSyntax p when p.IsKind(SyntaxKind.UnaryMinusExpression) && TryInt(p.Operand, out var x):
                v = -x; return true;
            case ParenthesizedExpressionSyntax pe:
                return TryInt(pe.Expression, out v);
            case CastExpressionSyntax ce:
                return TryInt(ce.Expression, out v);
        }
        return false;
    }

    void Class(ClassDeclarationSyntax c)
    {
        var baseType = c.BaseList?.Types.Select(t => t.Type).OfType<GenericNameSyntax>()
            .FirstOrDefault(g => g.Identifier.Text == "ValueEnum");
        if (baseType != null)
        {
            var d = Rec("value_enum", c);
            d["name"] = c.Identifier.Text;
            d["fqn"] = Fqn(c);
            d["type_args"] = baseType.TypeArgumentList.Arguments.Select(a => a.ToString()).ToList();
            var items = new List<object?[]>();
            var helpers = new List<object?[]>();
            foreach (var mem in c.Members)
            {
                if (mem is FieldDeclarationSyntax f && f.Modifiers.Any(SyntaxKind.StaticKeyword))
                {
                    foreach (var v in f.Declaration.Variables)
                    {
                        if (v.Initializer?.Value is BaseObjectCreationExpressionSyntax oc && oc.ArgumentList != null)
                        {
                            var args = oc.ArgumentList.Arguments;
                            var label = args.Count > 0 ? StringValue(args[0].Expression) : null;
                            var valueExpr = args.Count > 1 ? args[1].Expression.ToString() : null;
                            string? value = args.Count > 1 && TryInt(args[1].Expression, out var iv) ? iv.ToString() : null;
                            if (args.Count > 1 && args[1].Expression is LiteralExpressionSyntax sl && sl.IsKind(SyntaxKind.StringLiteralExpression))
                                value = sl.Token.ValueText;
                            items.Add(new object?[] { v.Identifier.Text, label, value, valueExpr, Line(v) });
                        }
                    }
                }
                else if (mem is MethodDeclarationSyntax m && !m.Modifiers.Any(SyntaxKind.StaticKeyword)
                         && m.ReturnType.ToString() == "bool")
                {
                    var body = m.Body?.ToString() ?? m.ExpressionBody?.ToString() ?? "";
                    helpers.Add(new object?[] { m.Identifier.Text, Short(body, 240), Line(m) });
                }
            }
            d["items"] = items;
            d["helpers"] = helpers;
        }

        // entity -> table: TableName = "X" anywhere in the class (normally Initialize())
        foreach (var a in c.DescendantNodes().OfType<AssignmentExpressionSyntax>())
        {
            if (a.Left is IdentifierNameSyntax { Identifier.Text: "TableName" } ||
                a.Left is MemberAccessExpressionSyntax { Name.Identifier.Text: "TableName", Expression: ThisExpressionSyntax })
            {
                var t = StringValue(a.Right);
                if (t != null && a.Ancestors().OfType<ClassDeclarationSyntax>().First() == c)
                {
                    var d = Rec("entity", a);
                    d["class"] = Fqn(c);
                    d["table"] = t;
                }
            }
        }
        // written columns: string[] _Fields = { ... } / new string[] { ... }
        foreach (var f in c.Members.OfType<FieldDeclarationSyntax>())
        {
            foreach (var v in f.Declaration.Variables)
            {
                if (!v.Identifier.Text.Contains("Fields")) continue;
                var init = v.Initializer?.Value switch
                {
                    InitializerExpressionSyntax ie => ie,
                    ArrayCreationExpressionSyntax ac => ac.Initializer,
                    ImplicitArrayCreationExpressionSyntax iac => iac.Initializer,
                    _ => null,
                };
                if (init == null) continue;
                var cols = init.Expressions.Select(StringValue).Where(s => s != null).ToList();
                if (cols.Count < 2) continue;
                var d = Rec("fields", v);
                d["class"] = Fqn(c);
                d["member"] = v.Identifier.Text;
                d["columns"] = cols;
            }
        }
    }

    void Const(FieldDeclarationSyntax f)
    {
        foreach (var v in f.Declaration.Variables)
        {
            if (v.Initializer == null) continue;
            var d = Rec("const", v);
            d["container"] = Fqn(f);
            d["name"] = v.Identifier.Text;
            d["type"] = f.Declaration.Type.ToString();
            string? value = TryInt(v.Initializer.Value, out var iv) ? iv.ToString() : StringValue(v.Initializer.Value);
            d["value"] = value;
            d["expr"] = Short(v.Initializer.Value.ToString(), 200);
        }
    }

    // ---- object initializers: MasterView / Report / MemType ------------------

    static string? TypeName(BaseObjectCreationExpressionSyntax oc) => oc switch
    {
        ObjectCreationExpressionSyntax o => o.Type switch
        {
            QualifiedNameSyntax q => q.Right.Identifier.Text,
            SimpleNameSyntax s => s.Identifier.Text,
            _ => o.Type.ToString(),
        },
        _ => null,
    };

    Dictionary<string, object?> Props(InitializerExpressionSyntax? init)
    {
        var props = new Dictionary<string, object?>();
        if (init == null) return props;
        foreach (var e in init.Expressions.OfType<AssignmentExpressionSyntax>())
        {
            var key = e.Left.ToString();
            if (e.Right is ObjectCreationExpressionSyntax or ArrayCreationExpressionSyntax or ImplicitArrayCreationExpressionSyntax
                or CollectionExpressionSyntax or InitializerExpressionSyntax)
                continue;    // nested collections are handled structurally
            var s = StringValue(e.Right);
            props[key] = s ?? Short(e.Right.ToString(), 200);
        }
        return props;
    }

    List<Dictionary<string, object?>> Children(SyntaxNode scope, string typeName, Func<SyntaxNode, bool>? within = null)
    {
        var list = new List<Dictionary<string, object?>>();
        foreach (var oc in scope.DescendantNodes().OfType<ObjectCreationExpressionSyntax>())
        {
            if (TypeName(oc) != typeName) continue;
            if (within != null && !within(oc)) continue;
            var p = Props(oc.Initializer);
            p["_line"] = Line(oc);
            p["_line_end"] = LineEnd(oc);
            list.Add(p);
        }
        return list;
    }

    void Creation(ObjectCreationExpressionSyntax oc)
    {
        var tn = TypeName(oc);
        if (tn == "MasterView" && oc.Initializer != null)
        {
            var d = Rec("view", oc);
            d["line_end"] = LineEnd(oc);
            d["props"] = Props(oc.Initializer);
            d["columns"] = Children(oc, "Column");
        }
        else if (tn == "Report" && oc.Initializer != null)
        {
            var d = Rec("report", oc);
            d["line_end"] = LineEnd(oc);
            d["props"] = Props(oc.Initializer);
            d["filters"] = Children(oc, "Filter");
            d["fields"] = Children(oc, "Column");
        }
    }

    void Invocation(InvocationExpressionSyntax inv)
    {
        // MemTypeCatalog.Register(new MemType("X").AddItem(...).AddItem(...))   (CoreAPI catalogue)
        // MemTypeCatalogue.RegisterMemType(new MemType("X").AddItem(...))        (Modern.Next catalogue)
        if (inv.Expression is MemberAccessExpressionSyntax { Name.Identifier.Text: "Register" or "RegisterMemType" } ma
            && (ma.Expression.ToString().EndsWith("MemTypeCatalog") || ma.Expression.ToString().EndsWith("MemTypeCatalogue"))
            && inv.ArgumentList.Arguments.Count == 1)
        {
            var chain = inv.ArgumentList.Arguments[0].Expression;
            var items = new List<object?[]>();
            string? name = null;
            var cur = chain;
            while (true)
            {
                if (cur is InvocationExpressionSyntax ci && ci.Expression is MemberAccessExpressionSyntax cma)
                {
                    if (cma.Name.Identifier.Text == "AddItem" && ci.ArgumentList.Arguments.Count >= 2)
                    {
                        var a = ci.ArgumentList.Arguments;
                        string? value = TryInt(a[1].Expression, out var iv) ? iv.ToString() : StringValue(a[1].Expression);
                        items.Add(new object?[] { StringValue(a[0].Expression) ?? a[0].Expression.ToString(), value, a[1].Expression.ToString(), Line(ci) });
                    }
                    cur = cma.Expression;
                    continue;
                }
                if (cur is ObjectCreationExpressionSyntax mo && TypeName(mo) == "MemType")
                    name = mo.ArgumentList?.Arguments.Count > 0 ? StringValue(mo.ArgumentList.Arguments[0].Expression) : null;
                break;
            }
            if (name != null)
            {
                items.Reverse();
                var d = Rec("memtype", inv);
                d["name"] = name;
                d["items"] = items;
            }
            return;
        }

        // reader.GetXxx("Column")
        if (inv.Expression is MemberAccessExpressionSyntax rma && ReaderMethods.Contains(rma.Name.Identifier.Text)
            && inv.ArgumentList.Arguments.Count >= 1 && StringValue(inv.ArgumentList.Arguments[0].Expression) is string col
            && Regex.IsMatch(col, @"^[A-Za-z_][A-Za-z0-9_]*$"))
        {
            var d = Rec("colread", inv);
            d["column"] = col;
            d["accessor"] = rma.Name.Identifier.Text;
            var cls = inv.Ancestors().OfType<ClassDeclarationSyntax>().FirstOrDefault();
            d["class"] = cls != null ? Fqn(cls) : null;
            // wrapper: T.resolve(<this>) / T.FromValue(<this>) / (T)<this>
            SyntaxNode n = inv;
            while (n.Parent is ParenthesizedExpressionSyntax) n = n.Parent;
            string? wrapper = null;
            if (n.Parent is CastExpressionSyntax cast) { wrapper = cast.Type.ToString(); n = cast; }
            else if (n.Parent is ArgumentSyntax arg && arg.Parent?.Parent is InvocationExpressionSyntax outer
                     && outer.Expression is MemberAccessExpressionSyntax om)
            {
                wrapper = om.Expression.ToString() + "." + om.Name.Identifier.Text;
                n = outer;
            }
            if (wrapper == null && rma.Name is GenericNameSyntax gn && gn.TypeArgumentList.Arguments.Count == 1)
            {
                var ta = gn.TypeArgumentList.Arguments[0].ToString();
                if (!Primitive.Contains(ta)) wrapper = ta;
            }
            d["wrapper"] = wrapper;
            // assignment target
            while (n.Parent is ParenthesizedExpressionSyntax or CastExpressionSyntax) n = n.Parent;
            string? target = null;
            if (n.Parent is AssignmentExpressionSyntax asg && asg.Right == n)
                target = asg.Left switch
                {
                    MemberAccessExpressionSyntax lm => lm.Name.Identifier.Text,
                    IdentifierNameSyntax id => id.Identifier.Text,
                    _ => asg.Left.ToString(),
                };
            else if (n.Parent is ArgumentSyntax a2 && a2.Parent?.Parent is ObjectCreationExpressionSyntax wrapNew)
                wrapper ??= "new " + wrapNew.Type;
            d["target"] = target;
            if (target != null && cls != null)
                d["target_type"] = MemberType(cls, target.TrimStart('_'));
            d["snippet"] = Short(inv.Ancestors().OfType<StatementSyntax>().FirstOrDefault()?.ToString() ?? inv.ToString(), 200);
        }
    }

    static string? MemberType(ClassDeclarationSyntax cls, string name)
    {
        foreach (var m in cls.Members)
        {
            if (m is PropertyDeclarationSyntax p && string.Equals(p.Identifier.Text, name, StringComparison.OrdinalIgnoreCase))
                return p.Type.ToString();
            if (m is FieldDeclarationSyntax f)
                foreach (var v in f.Declaration.Variables)
                    if (string.Equals(v.Identifier.Text.TrimStart('_'), name, StringComparison.OrdinalIgnoreCase))
                        return f.Declaration.Type.ToString();
        }
        return null;
    }

    // ---- SQL text in strings ---------------------------------------------------

    void Sql(SyntaxNode root)
    {
        // maximal string expressions: literal, interpolation, or + chain containing a literal
        foreach (var node in root.DescendantNodes())
        {
            if (node is not (LiteralExpressionSyntax or InterpolatedStringExpressionSyntax or BinaryExpressionSyntax)) continue;
            if (node is LiteralExpressionSyntax lit && !lit.IsKind(SyntaxKind.StringLiteralExpression)) continue;
            if (node is BinaryExpressionSyntax be && !be.IsKind(SyntaxKind.AddExpression)) continue;
            if (node.Parent is BinaryExpressionSyntax pb && pb.IsKind(SyntaxKind.AddExpression)) continue;   // not maximal
            if (node.Parent is ParenthesizedExpressionSyntax { Parent: BinaryExpressionSyntax pp } && pp.IsKind(SyntaxKind.AddExpression)) continue;
            var text = Template(node as ExpressionSyntax);
            // a string that is only a procedure name (string cmdtxt = "usp_x"; GetCommand(cmdtxt + " @a")) is an exec
            if (node is LiteralExpressionSyntax && text != null && BareProc.Match(text) is { Success: true } bp)
                text = "exec " + bp.Groups[1].Value;
            else if (text == null || text.Length < 12 || !SqlLike.IsMatch(text)) continue;
            var d = Rec("sql", node);
            var member = node.Ancestors().OfType<MemberDeclarationSyntax>().FirstOrDefault(m => m is not BaseTypeDeclarationSyntax);
            var cls = node.Ancestors().OfType<BaseTypeDeclarationSyntax>().FirstOrDefault();
            d["container"] = (cls != null ? Fqn(cls) : "") + (member switch
            {
                MethodDeclarationSyntax m => "." + m.Identifier.Text,
                PropertyDeclarationSyntax p => "." + p.Identifier.Text,
                ConstructorDeclarationSyntax c => ".ctor",
                _ => "",
            });
            d["text"] = text.Length > 6000 ? text[..6000] : text;
        }
    }

    static string? Template(ExpressionSyntax? e)
    {
        switch (e)
        {
            case null: return null;
            case LiteralExpressionSyntax l when l.IsKind(SyntaxKind.StringLiteralExpression):
                return l.Token.ValueText;
            case InterpolatedStringExpressionSyntax i:
            {
                var sb = new StringBuilder();
                foreach (var c in i.Contents)
                    sb.Append(c is InterpolatedStringTextSyntax t ? t.TextToken.ValueText : "{}");
                return sb.ToString();
            }
            case BinaryExpressionSyntax b when b.IsKind(SyntaxKind.AddExpression):
            {
                var l = Template(b.Left);
                var r = Template(b.Right);
                if (l == null && r == null) return null;
                return (l ?? "{}") + (r ?? "{}");
            }
            case ParenthesizedExpressionSyntax p:
                return Template(p.Expression);
            default:
                return null;
        }
    }
}
