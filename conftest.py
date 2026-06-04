"""Stellt sicher, dass das Repo-Root auf sys.path liegt, damit Tests
`utils.text`, `cogs.presets` etc. importieren können – egal von wo pytest läuft.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
