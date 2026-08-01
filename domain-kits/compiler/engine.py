"""Domain kit compiler: YAML sheet → BlockDef JSON + generated failure-mode tests."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_GENERATED = _REPO_ROOT / "domain-kits" / "generated"
_DEFAULT_TESTS_GENERATED = _REPO_ROOT / "tests" / "generated"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return cleaned.strip("_") or "unnamed"


def _block_def_cls():
    """Load BlockDef from the single-source schemas module (foundation-owned)."""
    from common.models.schemas import BlockDef

    cls = BlockDef
    if cls is None:
        raise ValueError("BlockDef schema class is unavailable")
    return cls


def _load_sheet(sheet_path: str | Path) -> dict[str, Any]:
    path = Path(sheet_path)
    if not path.is_file():
        raise FileNotFoundError(f"domain sheet not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"domain sheet must be a mapping: {path}")
    if "domain" not in data or not str(data["domain"]).strip():
        raise ValueError("domain sheet requires a non-empty 'domain' field")
    return data


def _json_schema_object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return schema


def _block_from_data_table(domain: str, table: dict[str, Any]) -> Any:
    BlockDef = _block_def_cls()
    name = _slug(str(table["name"]))
    block_id = f"{domain}_{name}_lookup"
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    lookup_key = table.get("lookup_key") or (columns[0]["name"] if columns else "key")
    col_props = {
        str(col["name"]): {"type": col.get("type", "string")}
        for col in columns
        if isinstance(col, dict) and "name" in col
    }
    row_summary = json.dumps(rows, sort_keys=True)
    prompt = (
        f"You are the {block_id} lookup block for domain '{domain}'. "
        f"Look up rows in the '{name}' table using '{lookup_key}'. "
        f"Known rows (authoritative when corroborated by evidence): {row_summary}. "
        f"Evidence standard: {table.get('evidence_standard', 'cite attached evidence')}."
    )
    return BlockDef(
        block_id=block_id,
        name=f"{name}_lookup",
        domain=domain,
        input_schema=_json_schema_object(
            {
                str(lookup_key): {"type": "string"},
                "evidence_excerpts": {"type": "array", "items": {"type": "string"}},
            },
            [str(lookup_key)],
        ),
        output_json_schema=_json_schema_object(
            {
                "matched": {"type": "boolean"},
                "row": {"type": "object", "properties": col_props},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["source", "excerpt"],
                    },
                },
            },
            ["matched", "row", "citations"],
        ),
        system_prompt_template=prompt,
        tools=list(table.get("tools") or ["file_reader"]),
        evidence_standard=table.get("evidence_standard"),
        dependencies=[],
        version_clock=1,
    )


def _block_from_calculation(domain: str, calc: dict[str, Any]) -> Any:
    BlockDef = _block_def_cls()
    name = _slug(str(calc["name"]))
    block_id = f"{domain}_{name}"
    formula = str(calc.get("formula") or "").strip()
    if not formula:
        raise ValueError(f"calculation '{name}' requires a formula")
    inputs = calc.get("inputs") or []
    outputs = calc.get("outputs") or []
    in_props = {
        str(item["name"]): {"type": item.get("type", "number")}
        for item in inputs
        if isinstance(item, dict) and "name" in item
    }
    out_props = {
        str(item["name"]): {"type": item.get("type", "number")}
        for item in outputs
        if isinstance(item, dict) and "name" in item
    }
    if not out_props:
        out_props = {"result": {"type": "number"}}
    required_in = list(in_props.keys())
    required_out = list(out_props.keys())
    prompt = (
        f"You are the {block_id} calculation block for domain '{domain}'. "
        f"Apply formula exactly: {formula}. "
        f"Do not invent inputs; ground every numeric operand in evidence. "
        f"Evidence standard: {calc.get('evidence_standard', 'cite attached evidence')}."
    )
    depends = [str(d) for d in (calc.get("depends_on") or [])]
    return BlockDef(
        block_id=block_id,
        name=name,
        domain=domain,
        input_schema=_json_schema_object(in_props, required_in),
        output_json_schema=_json_schema_object(out_props, required_out),
        system_prompt_template=prompt,
        tools=list(calc.get("tools") or ["calculator"]),
        evidence_standard=calc.get("evidence_standard"),
        dependencies=depends,
        version_clock=1,
    )


def _block_from_decision_rule(domain: str, rule: dict[str, Any]) -> Any:
    BlockDef = _block_def_cls()
    name = _slug(str(rule["name"]))
    block_id = f"{domain}_{name}_gate"
    condition = str(rule.get("condition") or "").strip()
    params = rule.get("parameters") or {}
    inputs = rule.get("inputs") or []
    in_props = {
        str(item["name"]): {"type": item.get("type", "number")}
        for item in inputs
        if isinstance(item, dict) and "name" in item
    }
    for key, value in params.items():
        in_props.setdefault(str(key), {"type": "number", "default": value})
    gate_schema = rule.get("output_schema") or {
        "type": "object",
        "required": ["allowed", "reason"],
        "properties": {
            "allowed": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "additionalProperties": False,
    }
    if set(gate_schema.get("required") or []) < {"allowed", "reason"}:
        raise ValueError(
            f"decision_rule '{name}' output_schema must require allowed and reason"
        )
    prompt = (
        f"You are the {block_id} decision gate for domain '{domain}'. "
        f"Evaluate condition: {condition}. "
        f"Parameters: {json.dumps(params, sort_keys=True)}. "
        f"Return JSON with boolean 'allowed' and string 'reason' only. "
        f"Evidence standard: {rule.get('evidence_standard', 'cite attached evidence')}."
    )
    return BlockDef(
        block_id=block_id,
        name=f"{name}_gate",
        domain=domain,
        input_schema=_json_schema_object(in_props, list(in_props.keys())),
        output_json_schema=gate_schema,
        system_prompt_template=prompt,
        tools=list(rule.get("tools") or ["calculator"]),
        evidence_standard=rule.get("evidence_standard"),
        dependencies=[],
        version_clock=1,
    )


def _render_failure_mode_test(domain: str, mode: dict[str, Any]) -> str:
    name = _slug(str(mode["name"]))
    description = str(mode.get("description") or "").strip()
    trigger = str(mode.get("trigger") or "").strip()
    expected = str(mode.get("expected_behavior") or "").strip()
    asserts = [str(a) for a in (mode.get("asserts") or [])]
    asserts_literal = json.dumps(asserts, indent=4)
    return (
        f'"""Generated failure-mode test for {domain}.{name}.\n'
        f"Auto-generated by domain_kits.compiler.engine — do not edit by hand.\n"
        f'"""\n'
        f"from __future__ import annotations\n\n"
        f"DOMAIN = {json.dumps(domain)}\n"
        f"FAILURE_MODE = {json.dumps(name)}\n"
        f"DESCRIPTION = {json.dumps(description)}\n"
        f"TRIGGER = {json.dumps(trigger)}\n"
        f"EXPECTED = {json.dumps(expected)}\n"
        f"ASSERTS = {asserts_literal}\n\n"
        f"def test_{domain}_{name}_failure_mode_documented():\n"
        f"    assert DOMAIN == {json.dumps(domain)}\n"
        f"    assert FAILURE_MODE == {json.dumps(name)}\n"
        f"    assert len(DESCRIPTION) > 0\n"
        f"    assert len(TRIGGER) > 0\n"
        f"    assert len(EXPECTED) > 0\n"
        f'    assert "refuse" in EXPECTED.lower() or "failure" in EXPECTED.lower()\n'
        f"    assert len(ASSERTS) >= 1\n"
        f"    assert all(isinstance(item, str) and len(item) > 0 for item in ASSERTS)\n"
        f"    assert ASSERTS == {asserts_literal}\n"
    )


def _write_block(block: Any, out_dir: Path) -> Path:
    # Round-trip through BlockDef to guarantee schema compliance.
    BlockDef = _block_def_cls()
    validated = BlockDef.model_validate(block.model_dump())
    path = out_dir / f"{validated.block_id}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(validated.model_dump(mode="json"), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def compile_sheet(
    sheet_path: str | Path,
    out_dir: str | Path | None = None,
    tests_out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compile a domain YAML sheet into BlockDef JSON files and failure-mode tests."""
    sheet = _load_sheet(sheet_path)
    domain = _slug(str(sheet["domain"]))
    target = Path(out_dir) if out_dir is not None else _DEFAULT_GENERATED
    tests_target = (
        Path(tests_out_dir) if tests_out_dir is not None else _DEFAULT_TESTS_GENERATED
    )
    target.mkdir(parents=True, exist_ok=True)
    tests_target.mkdir(parents=True, exist_ok=True)

    written_blocks: list[str] = []
    written_tests: list[str] = []

    for table in sheet.get("data_tables") or []:
        block = _block_from_data_table(domain, table)
        written_blocks.append(str(_write_block(block, target)))

    for calc in sheet.get("calculations") or []:
        block = _block_from_calculation(domain, calc)
        written_blocks.append(str(_write_block(block, target)))

    for rule in sheet.get("decision_rules") or []:
        block = _block_from_decision_rule(domain, rule)
        written_blocks.append(str(_write_block(block, target)))

    for mode in sheet.get("failure_modes") or []:
        mode_name = _slug(str(mode["name"]))
        test_path = tests_target / f"test_{domain}_{mode_name}.py"
        test_path.write_text(_render_failure_mode_test(domain, mode), encoding="utf-8")
        written_tests.append(str(test_path))

    if not written_blocks:
        raise ValueError(
            f"sheet {sheet_path} produced no BlockDefs; need data_table/calculation/decision_rule"
        )

    return {
        "domain": domain,
        "blocks": written_blocks,
        "tests": written_tests,
        "out_dir": str(target),
        "tests_out_dir": str(tests_target),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a domain kit YAML sheet")
    parser.add_argument(
        "--sheet",
        required=True,
        help="Path to domain YAML sheet (e.g. domain-kits/sheets/port_ops.yaml)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(_DEFAULT_GENERATED),
        help="Directory for generated BlockDef JSON files",
    )
    parser.add_argument(
        "--tests-out-dir",
        default=str(_DEFAULT_TESTS_GENERATED),
        help="Directory for generated failure-mode pytest files",
    )
    args = parser.parse_args(argv)
    compile_sheet(args.sheet, out_dir=args.out_dir, tests_out_dir=args.tests_out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
