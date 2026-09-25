"""Fixed project-local pytest adapter; generated test code stays in the container."""

import sys

import pytest

sys.path.insert(0, "/project")
raise SystemExit(pytest.main(["-q", "-c", "/dev/null", "--confcutdir=/project", "/project/tests"]))
