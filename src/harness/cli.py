import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="MD Harness")
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.parse_args()
    parser.print_help()
