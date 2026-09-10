"""Entrypoint — GhostVendor autonomous dependency resilience engineer."""

import argparse
import logging
import sys
from pathlib import Path

# Add src/ to path so all internal imports resolve as bare module names
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
from pipeline.state_machine import StateMachine
from utils.logging_config import configure as configure_logging

load_dotenv()

logger = logging.getLogger(__name__)


def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="GhostVendor — Autonomous dependency resilience engineer")
    parser.add_argument("repo", help="GitHub repository in owner/name format (e.g. acme/my-app)")
    parser.add_argument(
        "--triggered-by",
        metavar="GITHUB_LOGIN",
        help="GitHub login of the person who triggered this run — added as reviewer on the fix PR",
        default=None,
    )
    parser.add_argument(
        "--pr",
        metavar="PR_NUMBER",
        type=int,
        help="Triggering PR number — referenced in the fix PR body",
        default=None,
    )
    args = parser.parse_args()

    # Draft PR gate — exit immediately if the triggering PR is still a draft.
    # Streamlit monitor passes --pr when triggering; without it the gate is a no-op.
    if args.pr:
        from tools import github_client
        try:
            repo_obj = github_client.get_repo(args.repo)
            triggering_pr = repo_obj.get_pull(args.pr)
            if triggering_pr.draft:
                logger.info("Triggering PR #%d is a draft — skipping pipeline run", args.pr)
                sys.exit(0)
        except Exception as e:
            logger.warning("Could not check draft status of PR #%d: %s — proceeding", args.pr, e)

    machine = StateMachine(
        repo=args.repo,
        triggered_by=args.triggered_by,
        triggering_pr=args.pr,
    )
    machine.run()


if __name__ == "__main__":
    main()
