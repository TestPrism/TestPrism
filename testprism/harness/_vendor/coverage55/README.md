# coverage.py 5.5 vendored runtime

This directory contains the pure-Python `coverage` package from coverage.py
5.5. TestPrism copies it into disposable task containers so coverage
collection works offline and with Python 3.5 task environments.

Upstream: <https://github.com/nedbat/coveragepy/tree/coverage-5.5>

The upstream Apache 2.0 license and attribution are preserved in `LICENSE.txt`
and `NOTICE.txt`. The package is kept unmodified; native tracer sources are
present, but task containers use the pure-Python tracer because no extension is
built.
