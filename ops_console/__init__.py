"""Lucent local ops console — a standalone organization administration tool.

This package is deliberately **not** imported by the Lucent server. It is a
separate process you start when you want to administer a small Lucent instance,
and it talks to the server only over the public hyperadmin HTTP API. That
separation is the point:

* the server keeps no new attack surface — the console uses the same endpoints
  an external signup site would use, so anything the console can do, the eventual
  hosted site can do too;
* the console can be restarted, replaced, or deleted without touching Lucent;
* it is a test bed for the real management site rather than a second source of
  truth (it owns no database connection and no schema).

See ``docs/ops-console.md`` for how to run it.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
