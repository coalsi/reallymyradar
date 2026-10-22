"""Entry point for packaged builds (PyInstaller needs a plain script, not a package module)."""
from reallymyradar.app import main

if __name__ == "__main__":
    main()
