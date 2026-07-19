"""Environment assessment and capability adaptation pipeline."""

import sys

from . import pipeline as _pipeline

# Preserve the public ``daemon.adaptation`` module surface while keeping the
# implementation in a focused submodule. This also preserves module-level
# monkeypatching of paths and HTTP clients used by existing integrations.
sys.modules[__name__] = _pipeline
