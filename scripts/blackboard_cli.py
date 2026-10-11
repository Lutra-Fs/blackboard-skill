#!/usr/bin/env python3
"""Public Blackboard CLI entrypoint."""
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        'blackboard: Python 3.11+ required (running %s); use the skill venv: .venv/bin/python\n'
        % sys.version.split()[0]
    )
    raise SystemExit(4)

from blackboard.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
