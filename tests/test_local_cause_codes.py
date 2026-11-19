"""Literal Refused/Failed causes in src; dynamic calls have short reviewed exceptions."""
import ast
from pathlib import Path
from boring_outcomes import OUR_CODES

EXTRA_CODES = {
    "range": "an unsupported or unsatisfiable HTTP byte range (HTTP 416)",
    "checksum": "a deposit does not match its declared size or SHA-256; no publication",
    "storage": "upload publication could not be made durable",
    "upload": "an unexpected upload interruption; inspect transfer state",
}
DYNAMIC_CODES = {
    ("canvas/src/boring_canvas/mcp_service.py", "code"):
        "local refusal wrapper forwards its caller-owned cause",
}


def test_local_cause_vocabulary():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.glob("*/src/**/*.py"))
    assert paths and not OUR_CODES.keys() & EXTRA_CODES.keys()
    allowed, reviewed = OUR_CODES.keys() | EXTRA_CODES.keys(), set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name) else
                    call.func.attr if isinstance(call.func, ast.Attribute) else "")
            if name not in ("Refused", "Failed"):
                continue
            code = next((kw.value for kw in call.keywords if kw.arg == "code"),
                        call.args[0] if call.args else None)
            where = f"{relative}:{call.lineno}"
            if isinstance(code, ast.Constant):
                assert isinstance(code.value, str) and code.value in allowed, where
            else:
                key = (relative, ast.unparse(code))
                assert key in DYNAMIC_CODES and DYNAMIC_CODES[key], where
                reviewed.add(key)
    assert reviewed == DYNAMIC_CODES.keys()
