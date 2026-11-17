"""Run the possession pipeline from the repository root.

    python tiki_data_algorithm.py
    python tiki_data_algorithm.py 1 2 3 4 5 6

The implementation lives in starter/tiki_data_algorithm.py.
"""

import runpy
import sys
from pathlib import Path

starter = Path(__file__).resolve().parent / "starter" / "tiki_data_algorithm.py"
sys.argv[0] = str(starter)
runpy.run_path(str(starter), run_name="__main__")