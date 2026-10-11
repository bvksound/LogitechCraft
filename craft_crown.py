"""Entry point for PyInstaller and for running from a checkout: python craft_crown.py"""

import sys

from craftcrown.app import main

if __name__ == "__main__":
    sys.exit(main())
