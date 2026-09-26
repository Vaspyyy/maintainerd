"""Small structured report contract and readable rendering."""

from __future__ import annotations

import json

from .state import Error


TEXT = {"type": "string", "maxLength": 12000}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 30}
FINDING = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "problem": TEXT, "evidence": {**STRINGS, "minItems": 1},
        "proposal": TEXT, "tradeoffs": TEXT, "questions": STRINGS,
    },
    "required": ["title", "problem", "evidence", "proposal", "tradeoffs", "questions"],
}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "outcome": {"type": "string", "enum": ["propose", "no_action", "needs_context"]},
        "summary": TEXT,
        "findings": {"type": "array", "items": FINDING, "maxItems": 1},
        "inspected_paths": STRINGS, "limitations": STRINGS,
        "memory_notes": {**STRINGS, "maxItems": 5},
    },
    "required": ["outcome", "summary", "findings", "inspected_paths", "limitations", "memory_notes"],
}


def check(value: object, schema: dict, path: str = "report") -> None:
    """Validate the small schema subset above, without a runtime dependency."""
    kind = schema["type"]
    expected = {"object": dict, "array": list, "string": str, "boolean": bool}[kind]
    if not isinstance(value, expected):
        raise Error(f"{path} must be {kind}.")
    if "enum" in schema and value not in schema["enum"]:
        raise Error(f"Invalid value at {path}.")
    if kind == "object":
        properties = schema["properties"]
        if set(value) != set(schema["required"]):
            raise Error(f"{path} has missing or unknown fields.")
        for key, item in value.items():
            check(item, properties[key], f"{path}.{key}")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise Error(f"{path} contains too many or too few items.")
        for index, item in enumerate(value):
            check(item, schema["items"], f"{path}[{index}]")
    if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 12000):
        raise Error(f"{path} is too long or too short.")


def parse(raw: str) -> dict:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Error("Final Codex report was not valid JSON.") from exc
    check(result, SCHEMA)
    if result["outcome"] == "propose" and not result["findings"]:
        raise Error("A proposal must have an evidence-backed finding.")
    if result["outcome"] == "no_action" and result["findings"]:
        raise Error("A no_action report must not contain proposals.")
    return result


def markdown(
    result: dict,
    run_id: str,
    sha: str,
    limitations: list[str],
    publications: list[dict] | None = None,
) -> str:
    publications = publications or []
    lines = [f"# Maintenance report {run_id}", "", f"Outcome: **{result['outcome']}**",
             f"Commit inspected: `{sha}`", "", result["summary"], ""]
    if publications:
        lines.extend(["**Proposal routed to GitHub:**", ""])
        for item in publications:
            verb = "created" if item.get("mode") == "created" else "joined"
            lines.append(f"- {verb} #{item['issue_number']}: {item['issue_url']}")
        lines.extend(["", "**No code, branch or PR was published.**", ""])
    else:
        lines.extend(["**Local report only. No issue, comment, branch or PR was published.**", ""])
    for finding in result["findings"]:
        lines.extend([f"## {finding['title']}", "", finding["problem"], "", "### Evidence", ""])
        lines.extend(f"- {item}" for item in finding["evidence"])
        lines.extend(["", "### Possible proposal", "", finding["proposal"], "", "### Tradeoffs", "",
                      finding["tradeoffs"], "", "### Questions before implementation", ""])
        lines.extend(f"- {item}" for item in finding["questions"])
        lines.append("")
    lines.extend(["## Scope and limitations", ""])
    lines.extend(f"- {item}" for item in dict.fromkeys([*limitations, *result["limitations"]]))
    lines.extend(["", "## Paths inspected", ""])
    lines.extend(f"- `{item}`" for item in result["inspected_paths"])
    return "\n".join(lines) + "\n"
