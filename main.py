import argparse
from dotenv import load_dotenv
from src.pipeline.state_machine import StateMachine

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="GhostVendor — Autonomous dependency resilience engineer")
    parser.add_argument("repo", help="GitHub repository in owner/name format (e.g. acme/my-app)")
    args = parser.parse_args()

    machine = StateMachine(repo=args.repo)
    machine.run()


if __name__ == "__main__":
    main()
