"""Lives in stratlib.sim.strategies.weekend_trend, shared with the app's backtests. This module stands in for it
under the research scripts' import path, so they import the same objects."""

import sys

from stratlib.sim.strategies import weekend_trend as _module

sys.modules[__name__] = _module
