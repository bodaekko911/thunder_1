import re
from types import SimpleNamespace

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.routers.customers import customers_ui


def test_customers_page_keeps_csv_newline_escaped_for_browser_js() -> None:
    user = SimpleNamespace(
        id=1,
        name="Admin",
        email="admin@example.com",
        role="admin",
        permissions="*",
        is_active=True,
    )

    html = customers_ui(user)
    scripts = "\n".join(
        re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL)
    )

    assert 'join("\\n")' in scripts
    assert 'includes("\\n")' in scripts
    assert 'join("\n")' not in scripts
    assert 'includes("\n")' not in scripts
