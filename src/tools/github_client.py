import logging
import os
from github import Github, GithubException

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> Github:
    global _client
    if _client is None:
        _client = Github(os.environ["GITHUB_TOKEN"])
    return _client


def get_repo(repo: str):
    return _get_client().get_repo(repo)


def get_tree(repo: str) -> list[str]:
    r = get_repo(repo)
    tree = r.get_git_tree(r.default_branch, recursive=True)
    return [item.path for item in tree.tree if item.type == "blob"]


def get_file(repo: str, path: str) -> str:
    r = get_repo(repo)
    content = r.get_contents(path, ref=r.default_branch)
    return content.decoded_content.decode("utf-8")


def create_branch(repo: str, branch: str) -> str:
    r = get_repo(repo)
    source = r.get_branch(r.default_branch)
    candidate = branch
    n = 1
    while True:
        try:
            r.create_git_ref(ref=f"refs/heads/{candidate}", sha=source.commit.sha)
            return candidate
        except GithubException as e:
            if e.status == 422:
                n += 1
                candidate = f"{branch}-{n}"
            else:
                raise


def commit_file(repo: str, branch: str, path: str, content: str, message: str):
    r = get_repo(repo)
    try:
        existing = r.get_contents(path, ref=branch)
        r.update_file(path, message, content, existing.sha, branch=branch)
    except GithubException:
        r.create_file(path, message, content, branch=branch)


def create_pr(
    repo: str,
    branch: str,
    title: str,
    body: str,
    draft: bool = False,
    labels: list[str] | None = None,
    reviewer: str | None = None,
    base_branch: str | None = None,
) -> str:
    r = get_repo(repo)
    base = base_branch or r.default_branch
    pr = r.create_pull(title=title, body=body, head=branch, base=base, draft=draft)
    if labels:
        _ensure_labels(r, labels)
        pr.add_to_labels(*labels)
    if reviewer:
        try:
            pr.create_review_request(reviewers=[reviewer])
            logger.info("PR reviewer requested: %s", reviewer)
        except GithubException as e:
            logger.warning("Failed to add reviewer %s: %s", reviewer, e)
    return pr.html_url


def _ensure_labels(repo, label_names: list[str]) -> None:
    """Create any labels that don't yet exist on the repo."""
    try:
        existing = {lbl.name for lbl in repo.get_labels()}
        _LABEL_COLORS = {
            "ghostvendor": "0075ca",
            "automated": "e4e669",
            "resilience": "d93f0b",
            "ghostvendor-findings": "f9d0c4",
        }
        for name in label_names:
            if name not in existing:
                color = _LABEL_COLORS.get(name, "ededed")
                repo.create_label(name=name, color=color)
    except Exception:
        pass  # labels are cosmetic — never block PR creation


def dispatch_workflow(repo: str, workflow_id: str, branch: str, inputs: dict = None):
    r = get_repo(repo)
    workflow = r.get_workflow(workflow_id)
    workflow.create_dispatch(ref=branch, inputs=inputs or {})


def get_workflow_runs(repo: str, branch: str, workflow_id: str):
    r = get_repo(repo)
    workflow = r.get_workflow(workflow_id)
    return list(workflow.get_runs(branch=branch))


def get_run_logs(repo: str, run_id: int) -> str:
    r = get_repo(repo)
    run = r.get_workflow_run(run_id)
    return f"Run {run_id}: {run.conclusion} — {run.html_url}"
