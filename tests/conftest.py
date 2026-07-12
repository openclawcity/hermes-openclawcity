import sys
from pathlib import Path

# Make `occ_core` importable exactly the way the plugin loader does it:
# the plugin directory itself goes on sys.path.
PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
