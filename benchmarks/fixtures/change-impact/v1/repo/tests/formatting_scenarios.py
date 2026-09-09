"""Formatting scenarios representing test-side callsites."""

import impactlib.formatting as formatting
from impactlib.formatting import render as render_text


def check_render_invoice() -> str:
    """Exercise the invoice rendering scenario."""
    return formatting.render("a", style="bold")


def check_render_report() -> str:
    """Exercise the report rendering scenario."""
    return render_text("a", style="italic")


def check_render_label() -> str:
    """Exercise the label rendering scenario."""
    return formatting.render("a")


def check_render_alert() -> str:
    """Exercise the alert rendering scenario."""
    return render_text("a")
