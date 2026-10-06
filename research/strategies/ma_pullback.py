"""Lives in stratlib.sim.strategies.ma_pullback, beside the other shared strategies. This module stands in for it
under the research name, so the research scripts import the same objects."""

import sys

from stratlib.sim.strategies import ma_pullback as _module

sys.modules[__name__] = _module
