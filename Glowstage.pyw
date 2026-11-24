"""Double-click to start Glowstage without a console window."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glowstage.app import main  # noqa: E402

main()
