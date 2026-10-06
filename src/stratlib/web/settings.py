"""Settings: the screening and sell-rule thresholds, grouped by the rule each one changes. Saving rewrites only the
thresholds block of the config file, and refuses when the file changed since the form was loaded."""

from __future__ import annotations

import re
from dataclasses import asdict, fields
from html import escape

from nicegui import ui

from ..config import ConfigError, Thresholds, save_thresholds
from ..presentation import CHOICES, THRESHOLD_GROUPS
from ..strategies import STRATEGIES
from . import parts
from .data import Data, Workspace

DEFAULTS = {f.name: f.default for f in fields(Thresholds)}
LABELS = {key: label for entries in THRESHOLD_GROUPS.values() for key, label in entries}


def slug(text: str) -> str:
    return "group-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def plain(value) -> str:
    return f"{value:,g}" if isinstance(value, float) else f"{value:,}" if isinstance(value, int) else str(value)


class SettingsPage:
    """One tab's threshold editor."""

    def __init__(self, data: Data, workspace: Workspace):
        self.data, self.workspace = data, workspace
        self.flash: tuple[str, str, str | None] | None = None
        self.load()

    def load(self, *, defaults: bool = False):
        """Fill the form from the file, or with the code defaults; the file stays the comparison either way."""
        self.saved = asdict(self.data.settings.thresholds)
        self.values = asdict(Thresholds()) if defaults else dict(self.saved)

    def build(self):
        self.root = ui.element("div").classes("d-settings")
        self.render()

    def changed(self) -> list[str]:
        return [key for key, value in self.values.items() if value != self.saved[key]]

    def render(self):
        settings = self.data.settings
        self.root.clear()
        with self.root:
            with ui.element("section").classes("d-head"):
                with ui.element("div").classes("d-head-left"):
                    ui.html("Settings", sanitize=False, tag="h1").classes("d-title")
                with ui.element("div").classes("d-actions"):
                    parts.button("Reload saved", icon=parts.REDO, on_click=self.reload,
                                 title="Discard unsaved edits and read the config file again").mark("reload-settings")
                    parts.button("Load defaults", on_click=self.defaults,
                                 title="Fill the form with the code defaults; nothing is saved until you save").mark("load-defaults")
                    self.save_button = parts.button("Save thresholds", primary=True, on_click=self.save).mark("save-thresholds")
                ui.html("Percentages use percentage points: enter 25 for 25%. Changes apply after you save; Positions and the "
                        "N and M checks use them on their next view, and saved screens keep theirs until you run the screen "
                        "again.", sanitize=False, tag="p").classes("d-meta")
            self.notices = ui.element("div").classes("d-notices")
            self.show_notices()
            with ui.element("div").classes("d-settings-body"):
                with ui.element("nav").classes("d-card d-toc").props('aria-label="Threshold groups"'):
                    for group in THRESHOLD_GROUPS:
                        ui.html(escape(group), sanitize=False, tag="button").classes("d-toc-item").props(
                            f'type="button" onclick="document.getElementById(\'{slug(group)}\')'
                            ".scrollIntoView({behavior: 'smooth', block: 'start'})\"")
                    ui.html("Scan strategy parameters", sanitize=False, tag="button").classes("d-toc-item").props(
                        'type="button" onclick="document.getElementById(\'group-scan\').scrollIntoView({behavior: \'smooth\'})"')
                with ui.element("div").classes("d-settings-groups"):
                    for group, entries in THRESHOLD_GROUPS.items():
                        self.group(group, entries)
                    self.scan_parameters()
                    ui.html(f"Config file: {escape(str(settings.path.resolve()))}", sanitize=False, tag="p").classes("d-note")

    def show_notices(self):
        self.notices.clear()
        with self.notices:
            if self.flash:
                tone, text, detail = self.flash
                parts.notice(text, tone, detail=detail)
            edits = self.changed()
            if edits:
                parts.notice(f"{len(edits)} unsaved change{'' if len(edits) == 1 else 's'}.", "info",
                             detail="Save thresholds writes them to the config file.")

    def group(self, group, entries):
        with ui.element("section").classes("d-card d-section d-setting-group").props(
                f'id="{slug(group)}" aria-label="{escape(group)}"'):
            ui.html(escape(group), sanitize=False, tag="h3").classes("d-side-title")
            with ui.element("div").classes("d-setting-grid"):
                for key, label in entries:
                    self.field(key, label)

    def field(self, key, label):
        wrapper = ui.element("label").classes("d-setting" + (" is-changed" if self.values[key] != self.saved[key] else ""))
        with wrapper:
            ui.html(f'{escape(label)}<em>edited</em>', sanitize=False, tag="span").classes("d-setting-label")
            if key in CHOICES:
                ui.select(CHOICES[key], value=self.values[key], on_change=lambda e, key=key, wrapper=wrapper:
                          self.edit(key, e.value, wrapper)).mark(f"threshold-{key}").classes("d-field").props(
                    'dense outlined hide-bottom-space options-dense popup-content-class="d-popup"')
            else:
                integer = isinstance(DEFAULTS[key], int)
                ui.number(value=self.values[key], min=0, step=1 if integer else 0.1, format="%d" if integer else None,
                          on_change=lambda e, key=key, wrapper=wrapper: self.edit(key, e.value, wrapper)).mark(
                    f"threshold-{key}").classes("d-field").props(
                    f'dense outlined hide-bottom-space aria-label="{escape(label)}"')
            ui.html(f"Saved {escape(plain(self.saved[key]) if key not in CHOICES else CHOICES[key][self.saved[key]])}"
                    f" · default {escape(plain(DEFAULTS[key]) if key not in CHOICES else CHOICES[key][DEFAULTS[key]])}",
                    sanitize=False, tag="small").classes("d-setting-hint")

    def scan_parameters(self):
        settings = self.data.settings
        with ui.element("section").classes("d-card d-section d-setting-group").props('id="group-scan" aria-label="Scan strategy parameters"'):
            ui.html("Scan strategy parameters", sanitize=False, tag="h3").classes("d-side-title")
            ui.html("Qullamaggie, Minervini, Episodic Pivot, the 9/21 EMA trade, the Traveling Trader checklist and the Nash "
                    "screen read their rules from the strategies section of the config file. Every default is the "
                    "configuration its research tested. Edit the file, then run the screen again; saved positions keep the "
                    "values they were opened with.", sanitize=False, tag="p").classes("d-note")
            for strategy_id, params in settings.strategies.items():
                parts.details(f"{STRATEGIES[strategy_id].name} · strategies.{strategy_id}", parts.settings_html(asdict(params)))

    # Events -----------------------------------------------------------------------------------------------------

    def edit(self, key, value, wrapper):
        if key not in CHOICES and value is not None and isinstance(DEFAULTS[key], int) and float(value).is_integer():
            value = int(value)
        self.values[key] = value
        if value != self.saved[key]:
            wrapper.classes(add="is-changed")
        else:
            wrapper.classes(remove="is-changed")
        self.flash = None
        self.show_notices()

    def save(self):
        missing = [LABELS[key] for key, value in self.values.items() if value is None]
        if missing:
            self.flash = ("error", "Settings were not saved.", f"Enter a value for {missing[0]}.")
            self.show_notices()
            return
        path = self.data.settings.path
        try:
            thresholds = Thresholds(**self.values)
            save_thresholds(path, thresholds, expected=Thresholds(**self.saved))
        except (ConfigError, OSError) as exc:
            message = str(exc)
            for key, label in LABELS.items():
                message = message.replace(f"thresholds.{key}", f"“{label}”")
            self.flash = ("error", "Settings were not saved.", message)
            self.show_notices()
            return
        self.data.reload()
        self.load()
        self.flash = ("done", "Thresholds saved.", "Reopen Positions for updated alerts; run the screen again for new results.")
        self.render()

    def reload(self):
        try:
            self.data.reload()
        except ConfigError as exc:
            self.flash = ("error", "The config file could not be read.", str(exc))
            self.show_notices()
            return
        self.load()
        self.flash = ("done", "Saved settings reloaded.", None)
        self.render()

    def defaults(self):
        self.load(defaults=True)
        self.flash = ("info", "Defaults loaded into the form.", "Nothing is saved until you save thresholds.")
        self.render()
