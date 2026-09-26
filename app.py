"""Entry point for running from source (`py app.py`) and for the PyInstaller build."""
import sys

from ckmapviewer.cli import main

if __name__ == "__main__":
    sys.exit(main())
