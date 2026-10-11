from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from blackboard import course_service
from blackboard import providers

class RegistryTests(unittest.TestCase):
    def test_registry_covers_every_kind_exactly_once(self) -> None:
        # The CLI validates --kind against course_service.KINDS; fetch
        # dispatches through providers.REGISTRY. A kind missing here would
        # KeyError at fetch time, a kind missing there would be unfetchable.
        self.assertEqual(set(providers.REGISTRY), set(course_service.KINDS))


if __name__ == "__main__":
    unittest.main()
