"""Quick script to inspect callout node attrs for a specific content asset."""
import json
import sys
from sqlalchemy import text
from src.db.base import get_engine

ASSET_NAME_LIKE = sys.argv[1] if len(sys.argv) > 1 else "Demonstrated need"

engine = get_engine()
with engine.connect() as conn:
    row = conn.execute(
        text("""
            SELECT id, name, content
            FROM content_assets
            WHERE name ILIKE :name
            ORDER BY updated_at DESC
            LIMIT 1
        """),
        {"name": f"%{ASSET_NAME_LIKE}%"},
    ).fetchone()

if not row:
    print(f"No asset found matching: {ASSET_NAME_LIKE}")
    sys.exit(1)

asset_id, name, content_str = row
print(f"\nAsset: {name}")
print(f"ID   : {asset_id}")

if not content_str:
    print("content: NULL")
    sys.exit(0)

doc = json.loads(content_str)
nodes = doc.get("content", [])
print(f"Blocks: {len(nodes)}\n")

for i, node in enumerate(nodes):
    ntype = node.get("type")
    if ntype == "callout":
        attrs = node.get("attrs", {})
        print(f"[{i}] CALLOUT attrs:")
        for k, v in attrs.items():
            print(f"     {k}: {v!r}")
    elif ntype == "rawHtml":
        html = node.get("attrs", {}).get("html", "")[:120].replace("\n", " ")
        print(f"[{i}] rawHtml: {html!r}")
    else:
        # Show first 80 chars of text content
        text_preview = ""
        for child in node.get("content", []):
            for inline in child.get("content", []):
                text_preview += inline.get("text", "")
            if len(text_preview) > 80:
                break
        print(f"[{i}] {ntype}: {text_preview[:80]!r}")
