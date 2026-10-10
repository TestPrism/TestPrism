"""External integration entry point: python -B -m integrations.testprism."""
import sys

sys.dont_write_bytecode = True

from .runner import main

raise SystemExit(main())
