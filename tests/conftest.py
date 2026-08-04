import os
from pathlib import Path
import sys


os.environ.setdefault("STUDIO_SHOTGUN_LINK", "https://shotgrid.invalid")

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_ROOT))
