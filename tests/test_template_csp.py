"""Static checks for template patterns incompatible with the site's CSP."""

import re
from pathlib import Path


TEMPLATE_ROOT = Path(__file__).parents[1] / "src" / "lucent" / "web" / "templates"
INLINE_EVENT_ATTRIBUTE = re.compile(r"(?<![\w.])on[a-z]+\s*=", re.IGNORECASE)


def test_templates_do_not_contain_inline_event_handlers() -> None:
    """Event handlers must be registered by nonce-bearing scripts, not HTML attributes."""
    offenders = []
    for template in TEMPLATE_ROOT.rglob("*.html"):
        for match in INLINE_EVENT_ATTRIBUTE.finditer(template.read_text()):
            line = template.read_text()[: match.start()].count("\n") + 1
            offenders.append(f"{template.relative_to(TEMPLATE_ROOT)}:{line}: {match.group()}")

    assert not offenders, "Inline event handlers violate the application's nonce-only CSP:\n" + "\n".join(offenders)