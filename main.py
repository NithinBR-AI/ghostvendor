"""Entrypoint — GhostVendor autonomous dependency resilience engineer."""

import argparse
import sys
from pathlib import Path

# Add src/ to path so all internal imports resolve as bare module names
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
from pipeline.state_machine import StateMachine
from utils.logging_config import configure as configure_logging

load_dotenv()


def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="GhostVendor — Autonomous dependency resilience engineer")
    parser.add_argument("repo", help="GitHub repository in owner/name format (e.g. acme/my-app)")
    args = parser.parse_args()

    machine = StateMachine(repo=args.repo)
    machine.run()


if __name__ == "__main__":
    main()
