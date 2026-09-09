"""Production formatting call shapes."""

import impactlib.formatting as formatting
from impactlib.formatting import render as render_text


def render_invoice() -> str:
    """Render an invoice through the module API."""
    return formatting.render("a", style="bold")


def render_report() -> str:
    """Render a report through the direct API."""
    return render_text("a", style="italic")


def render_label() -> str:
    """Render a label through the module API."""
    return formatting.render("a")


def render_alert() -> str:
    """Render an alert through the direct API."""
    return render_text("a")
