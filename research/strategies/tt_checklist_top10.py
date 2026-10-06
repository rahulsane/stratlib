"""Moved to stratlib.sim.strategies.tt_checklist_top10, shared with the app's backtests. This module stands in for it
under its old name, so the research scripts import the same objects."""

import sys

from stratlib.sim.strategies import tt_checklist_top10 as _module

sys.modules[__name__] = _module
