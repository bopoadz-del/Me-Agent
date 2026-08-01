"""Tool registry: calculator, file_reader, web_search, vector_search."""
from __future__ import annotations

import ast
import json
import operator as op
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.Mod: op.mod,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


class AirGapError(RuntimeError):
    """Raised when a network tool is invoked in airgap mode."""


def calculator(expression: str) -> float:
    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](ev(node.left), ev(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](ev(node.operand)))
        raise ValueError(f"disallowed expression node: {type(node).__name__}")

    return ev(ast.parse(expression, mode="eval"))


def _allowed_evidence_roots() -> list[Path]:
    data_dir = os.environ.get("DATA_DIR", "/data")
    roots = [
        Path(data_dir) / "evidence",
        Path("/tmp/evidence"),
    ]
    return [p.resolve() for p in roots]


def file_reader(path: str) -> str:
    real = Path(os.path.realpath(path))
    allowed = False
    for root in _allowed_evidence_roots():
        try:
            real.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise PermissionError(f"path not in evidence whitelist: {path}")
    return real.read_text(encoding="utf-8")


def web_search(query: str) -> list[dict[str, Any]]:
    if os.environ.get("AIRGAP", "false").lower() == "true":
        raise AirGapError("web_search blocked in airgap mode")
    import httpx

    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_html": 1}
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
    related = data.get("RelatedTopics") or []
    results: list[dict[str, Any]] = []
    for item in related[:5]:
        if isinstance(item, dict) and "Text" in item:
            results.append({"text": item.get("Text"), "url": item.get("FirstURL")})
    return results


def vector_search(query_embedding: list[float], db_path: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        rows = conn.execute(
            """
            SELECT m.delta_id, m.key, m.value, v.distance
            FROM memories_vec v
            JOIN memory_deltas m ON m.rowid = v.rowid
            WHERE v.embedding MATCH ?
            ORDER BY v.distance
            LIMIT ?
            """,
            (json.dumps(query_embedding), limit),
        ).fetchall()
    except sqlite3.Error:
        rows = conn.execute(
            "SELECT delta_id, key, value FROM memory_deltas LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)
        out.append({"delta_id": row["delta_id"], "key": row["key"], "value": value})
    return out


TOOL_REGISTRY: dict[str, Any] = {
    "calculator": calculator,
    "file_reader": file_reader,
    "web_search": web_search,
    "vector_search": vector_search,
}
