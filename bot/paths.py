"""Repository locations. Modules locate files from here, never from their own
__file__, so moving a module never moves where it reads or writes."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
