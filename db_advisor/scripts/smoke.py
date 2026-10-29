"""Call every tool on example-shaped inputs; print status, size and a preview."""
import json
import sys

from mcp.server import MCPServer

from senior_dev_db.config import load_config
from senior_dev_db.tools import register_all
from senior_dev_db.tools.common import Ctx

CALLS = [
    ("trace_screen", {"target": "https://app.example.com/ui/manage/views/returnrequestview.aspx"}),
    ("trace_screen", {"target": "https://app.example.com/ui/reports/returnrequest.aspx"}),
    ("trace_screen", {"target": "Stock Adjustments Report"}),
    ("find_db_object", {"query": "Channel Type"}),
    ("find_db_object", {"query": "gift card"}),
    ("describe_column", {"table": "OrderTable.Status"}),
    ("describe_column", {"table": "PaymentLogTable.PayMethod"}),
    ("describe_column", {"table": "OrderChargesTable.NetChargeLocal"}),
    ("get_value_map", {"query": "PaymentLogTable.PaymentType"}),
    ("get_object_outline", {"name": "ShipmentView"}),
    ("get_object_outline", {"name": "usp_rpt_creditcardjournal"}),
    ("find_join_path", {"from_table": "IssueTable", "to_table": "OrderTable"}),
    ("get_impact", {"name": "ReturnRequestView", "column": "InvoiceDateClean"}),
    ("describe_table", {"name": "InvoiceHeaderTable", "columns_like": "date"}),
    ("get_house_style", {"kind": "report_proc", "topic": "currency"}),
    ("find_column_usage", {"table": "OrderChargesTable.NetChargeLocal", "max": 10}),
    ("search_sql", {"pattern": "PayMethod = 6", "max": 5}),
    ("validate_sql", {"sql": "SET ANSI_NULLS ON\nGO\nALTER VIEW [dbo].[ShipmentView] AS SELECT c.ChannelType, c.NoSuch FROM ProductTable c\nGO"}),
    ("index_status", {}),
]


def main():
    cfg = load_config()
    mcp = MCPServer("smoke")
    register_all(mcp, Ctx.create(cfg))
    only = sys.argv[1:]
    for name, kw in CALLS:
        if only and name not in only:
            continue
        out = mcp._tool_manager.get_tool(name).fn(**kw)
        d = out.model_dump(exclude_defaults=True)
        s = json.dumps(d, ensure_ascii=False)
        print(f"=== {name} {kw} -> {d.get('status')} [{len(s)} bytes]")
        print(json.dumps(d, ensure_ascii=False, indent=1)[:int(sys.argv[0] and 3500)])


if __name__ == "__main__":
    main()
