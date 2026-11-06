# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "anker-solix-api @ https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz",
#     "matplotlib",
# ]
# ///
"""Anker SOLIX power station watchdog with a Telegram bot; the code is in the ankerups package.

Run: uv run anker_ups.py --help (dependencies come from the header above).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the ankerups package next to this file

from ankerups.app import main  # noqa: E402

if __name__ == "__main__":
    main()
