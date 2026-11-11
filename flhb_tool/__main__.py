"""Allow running the package as: python -m flhb_tool"""

from .system_check import main
import sys

if __name__ == "__main__":
    sys.exit(main())
