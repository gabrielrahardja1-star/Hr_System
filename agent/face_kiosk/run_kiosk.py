"""PyInstaller entry point — packages the kiosk web app into a standalone exe.

`python -m facekiosk.app` is the dev entry; PyInstaller needs a plain script.
"""

from facekiosk.app import main

if __name__ == "__main__":
    raise SystemExit(main())
