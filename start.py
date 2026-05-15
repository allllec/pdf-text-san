"""Convenience launcher.

Usage:
  python start.py
  python start.py --port 9000 --host 0.0.0.0
"""

from app.cli import main


if __name__ == "__main__":
    main()
