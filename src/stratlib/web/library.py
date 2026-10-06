"""Pieces every workspace page shares: the strategy picker and the strategy's rules and research."""

from __future__ import annotations

from html import escape

from nicegui import ui

from ..evidence import evidence_rows, verdict as research_verdict
from ..presentation import UNSUPPORTED, rules_text, sizing_text, supported_names
from ..reports import load_catalog
from ..strategies import STRATEGIES, current_rules, market_policy, revision
from . import parts


def strategy_select(value: str, on_change, *, disabled: bool = False):
    """The workspace strategy, shared by every page that reads it."""
    options = {key: f"{s.name} · {s.variant_name}" for key, s in STRATEGIES.items()}
    return ui.select(options, value=value, on_change=lambda e: on_change(e.value)).mark("strategy").classes(
        "d-field d-strategy").props('dense outlined hide-bottom-space options-dense popup-content-class="d-popup" '
                                    'aria-label="Strategy"' + (" disable" if disabled else ""))


def strategy_meta(spec, settings) -> list[str]:
    """The strategy's variant and current rules revision, as meta-line parts."""
    variant = (f"{escape(spec.family)} · {escape(spec.variant_name)}" if spec.family and spec.family != spec.name
               else escape(spec.variant_name))
    return [variant, f"Rules <b>{revision(current_rules(settings, spec.id))}</b>"]


def strategy_library(settings, spec, *, public: bool = False):
    """The strategy's rules, sizing, market policy and research evidence, closed until asked for. The public app
    leaves out the notes about editing the rules."""
    fills = ("The backtest trades at review closes; alerts use completed closes." if spec.id == "msci_garp" else
             f"{'Research' if spec.scan else 'Backtest'} stop fills use intraday lows and gaps; live alerts use closes.")
    body = [f"<p><b>{escape(spec.name)} / {escape(spec.variant_name)}.</b> {escape(spec.description)}</p>",
            f"<p>{escape(rules_text(spec, settings))}</p>",
            f"<p>{escape(sizing_text(spec, settings))} Evaluate after completed daily closes"
            + ("; backtests execute eligible orders at the next open. " if not spec.scan else
               "; the research fills orders under its own ground rules. ")
            + escape(market_policy(spec.id, settings.backtest, settings)) + "</p>",
            f'<p class="d-note">{fills}</p>' if public else
            '<p class="d-note">Settings configure new screens and entries. Existing positions retain their saved rules. '
            + fills + (f" Edit the parameters under strategies.{escape(spec.id)} in config.yaml." if spec.scan else "")
            + "</p>"]
    if spec.scan:
        rows = evidence_rows(spec.id)
        if rows:
            note = research_verdict(spec.id)
            body.append(f"<h4>Research evidence</h4><p>{escape(note or '')}</p>")
            columns = list(rows[0])
            numeric = {key for key in columns if key not in {"Run", "Period"}}
            shown_rows = [{key: (f"{value:,.2f}" if isinstance(value, float) else value) for key, value in row.items()}
                          for row in rows]
            body.append(parts.table_html(shown_rows, [(key, key) for key in columns], numeric=numeric))
            body.append('<p class="d-note">Saved research runs under the ground rules in research/README.md. '
                        "They describe history, not a forecast.</p>")
        reports, _ = load_catalog()
        titles = {r.slug: r.title for r in reports}
        links = [f'<a href="/reports?report={slug}">{escape(titles[slug])}</a>' for slug in spec.reports if slug in titles]
        body.append(f'<p class="d-links">{" · ".join(links) or ""}</p>')
    body.append(f'<p class="d-links"><a href="/backtest">Backtest this strategy</a> · <a href="/reports">Browse research '
                f'reports</a></p><p class="d-note">Supported: {escape(supported_names())}. {escape(UNSUPPORTED)}</p>')
    parts.details("Strategy rules and research", "".join(body))
