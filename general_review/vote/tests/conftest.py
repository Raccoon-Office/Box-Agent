import sys
from pathlib import Path

# The vote review is repository tooling, not a package; import its modules as siblings.
VOTE_DIR = Path(__file__).resolve().parents[1]
if str(VOTE_DIR) not in sys.path:
    sys.path.insert(0, str(VOTE_DIR))
