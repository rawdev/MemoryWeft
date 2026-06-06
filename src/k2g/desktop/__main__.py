"""``python -m k2g.desktop`` entry — delegates to :func:`k2g.desktop.cli.main`."""

from __future__ import annotations

import sys

from k2g.desktop.cli import main

if __name__ == "__main__":
    sys.exit(main())
