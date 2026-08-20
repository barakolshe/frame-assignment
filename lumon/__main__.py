"""`python -m lumon <schedule.json>`."""

import sys

from lumon.cli import main

raise SystemExit(main(sys.argv[1:]))
