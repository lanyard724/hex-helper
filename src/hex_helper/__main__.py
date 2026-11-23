"""Package entrypoint enabling `python -m hex_helper` and `pythonw -m hex_helper`."""

from hex_helper.main import main

if __name__ == "__main__":
    raise SystemExit(main())
