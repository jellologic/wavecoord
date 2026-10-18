"""Shared paths for the tests. Puts src/ on sys.path."""

import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
SRC = os.path.join(ROOT, "src")
FIXTURES = os.path.join(TESTS, "fixtures")
SCHEMAS = os.path.join(ROOT, "schemas")

if SRC not in sys.path:
    sys.path.insert(0, SRC)
