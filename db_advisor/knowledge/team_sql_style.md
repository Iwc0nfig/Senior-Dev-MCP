# Team SQL style (example: each client's DB developers write their own)

These rules are authoritative. Where existing code is formatted differently, new and changed
SQL follows these rules. `get_house_style` returns them first, and `validate_sql` reports
deviations as style warnings.

## Rules

- Write SQL keywords and functions in lowercase: `select`, `from`, `where`, `inner join`, `case`, `isnull`, `convert`.
- Preserve actual PascalCase table and column names.
- Use short table aliases, such as `o`, `c`, `p`.
- Use consistent four-space indentation.
- Put each selected column on a separate line, except the first one, which goes next to the `select`.
- Put commas at the beginning of subsequent lines, followed by a space.
- Put each join on a separate line.
- Put additional conditions on separate lines beginning with `and` or `or`.
- Use `cross join` instead of `inner join ... on 1 = 1`.
- Write table hints after the alias: `InvoiceHeaderTable i with(nolock)`.

## Example

```sql
;with OpenOrderIds as (
    select o.Id
    from OrderTable o (nolock)
    where o.Cancelled = 0
      and o.Invoiced = 0
      and o.Total != 0
)
select o.RefNo as OrderNumber
    , o.NetAmount as LineAmount
    , c.Name as CustomerName
from OrderTable o (nolock)
    inner join OpenOrderIds ids on ids.Id = o.Id
    left outer join CustomerTable c (nolock) on c.Id = o.CustomerTable_Id
order by o.Id;
```
