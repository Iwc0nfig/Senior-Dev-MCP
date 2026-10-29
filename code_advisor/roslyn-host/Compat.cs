#if NET48
namespace System.Runtime.CompilerServices
{
    /// <summary>
    /// Shim so C# records and init-only setters compile on .NET Framework 4.8,
    /// which lacks this type.  The compiler only needs it to exist.
    /// </summary>
    internal static class IsExternalInit
    {
    }
}
#endif
