"""Entry point for the frozen (PyInstaller) backend used by the desktop app.

It is the same `form-filler` command line; the desktop shell runs it as
`form-filler serve --skip-setup`. freeze_support() must come first so the spawned
job-worker processes work inside the frozen build.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app.cli import main

    sys.exit(main())
