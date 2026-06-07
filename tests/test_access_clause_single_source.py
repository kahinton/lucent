"""Guard test: the ownership ACL predicate has a single source of truth.

``build_access_clause`` in ``lucent.access_control`` is the only place allowed to
spell out the raw ownership ``WHERE`` fragment (``owner_user_id = ... OR
owner_group_id IN ...``). Any other module that hand-rolls the predicate will
drift from the canonical resolution order and silently create access-control
holes. This test walks the source tree and fails if the raw pattern reappears
outside the sanctioned module.
"""

import re
from pathlib import Path

import lucent

# Raw predicates that indicate a hand-rolled resource-ownership ACL resolution
# clause rather than a call to build_access_clause. The group-visibility branch
# is the reliable fingerprint: only build_access_clause is allowed to emit the
# ``owner_group_id`` membership check that participates in the built-in ->
# owner_user -> owner_group -> org-shared resolution order. (A bare
# ``owner_user_id = $N`` filter is intentionally NOT flagged — it is also used
# for single-owner lookups on unrelated tables such as enterprise_credentials.)
_RAW_PATTERNS = [
    re.compile(r"owner_group_id\s+IN\s*\(\s*SELECT\s+group_id\s+FROM\s+user_groups", re.IGNORECASE),
    re.compile(r"owner_group_id\s*=\s*ANY\s*\(", re.IGNORECASE),
]

# Files allowed to contain the raw predicate.
_ALLOWED = {
    "access_control.py",  # the single source of truth itself
}


def _python_sources() -> list[Path]:
    root = Path(lucent.__file__).resolve().parent
    return [p for p in root.rglob("*.py") if p.name not in _ALLOWED]


def test_no_raw_ownership_predicate_outside_access_control() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in _RAW_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path}:{line_no}: {match.group(0)!r}")
    assert not offenders, (
        "Raw ownership ACL predicate found outside access_control.py. "
        "Use build_access_clause() instead:\n" + "\n".join(offenders)
    )
