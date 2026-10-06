"""Moved to stratlib.sim.strategies.qullamaggie, shared with the app's backtests. This module stands in for it
under its old name, so the research scripts import the same objects."""

import sys

from stratlib.sim.strategies import qullamaggie as _module

sys.modules[__name__] = _module
