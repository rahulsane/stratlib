"""Moved to stratlib.sim.market_filters, shared with the app's backtests. This module stands in for it under its old
name, so the research scripts import the same objects."""

import sys

from stratlib.sim import market_filters as _module

sys.modules[__name__] = _module
