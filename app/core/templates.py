"""
Centralized Jinja2 templates configuration.

All HTML page rendering should go through ``templates.TemplateResponse(...)``
defined here.  This keeps templates discoverable in one place
(``app/templates/``) and lets us add custom filters or globals (currency
formatting, date formatting, brand strings) in a single location.

Usage:

    from app.core.templates import templates

    @router.get("/invoice/{invoice_id}/print", response_class=HTMLResponse)
    async def print_invoice(invoice_id: int, request: Request, ...):
        return templates.TemplateResponse(
            request,
            "b2b_invoice_print.html",
            {"invoice": invoice, ...},
        )

The ``request`` argument is required by Starlette's TemplateResponse so that
``url_for`` and request-scoped state are available inside templates.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

# Templates live at <repo_root>/app/templates/, sibling to app/static/.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Custom filters ──────────────────────────────────────────────────────────
# Keep these small and presentation-only.  Anything that needs DB access or
# multi-step business logic belongs in a service, not a template filter.

def _egp(value) -> str:
    """Format a numeric value as 'EGP 1,234.56'."""
    try:
        return f"EGP {float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "EGP 0.00"


def _ar_egp(value) -> str:
    """Format a numeric value as 'ج.م. 1,234.56' (Arabic currency mark)."""
    try:
        return f"ج.م. {float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "ج.م. 0.00"


def _shortdate(value) -> str:
    """Format a datetime/date as '21-Apr-26', or '—' if missing."""
    if not value:
        return "—"
    try:
        return value.strftime("%d-%b-%y")
    except AttributeError:
        return str(value)


def _longdate(value) -> str:
    """Format a datetime/date as '21-Apr-2026', or '—' if missing."""
    if not value:
        return "—"
    try:
        return value.strftime("%d-%b-%Y")
    except AttributeError:
        return str(value)


def _humanize_snake(value) -> str:
    """Convert 'cash_on_delivery' -> 'Cash On Delivery'."""
    if not value:
        return ""
    return str(value).replace("_", " ").title()


templates.env.filters["egp"] = _egp
templates.env.filters["ar_egp"] = _ar_egp
templates.env.filters["shortdate"] = _shortdate
templates.env.filters["longdate"] = _longdate
templates.env.filters["humanize_snake"] = _humanize_snake


# ── Brand globals ───────────────────────────────────────────────────────────
# Constants pulled out of the inline HTML so a future logo/brand/registry
# change does not require editing three printable templates.

templates.env.globals["BRAND"] = {
    "company_name": "Habiba Organic Farm",
    "tagline": "Desert going green",
    "instagram": "habibaorganicfarm",
    "website": "habibacommunity.com",
    "commercial_registry": "126278",
    "tax_id": "560042604",
    "logo_path": "/static/Logo.png",
    "primary_color": "#2a7a2a",
}
