import argparse

from siteaudit import __version__


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="siteaudit",
        description="Concurrent website crawler and link-health auditor.",
    )
    parser.add_argument("--version", action="version", version=f"siteaudit {__version__}")
    parser.parse_args()
    return 0