import os
from github import Github, GithubException

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


def create_branch(repo: str, branch: str):
    r = get_repo(repo)
    source = r.get_branch(r.default_branch)
    r.create_git_ref(ref=f"refs/heads/{branch}", sha=source.commit.sha)


def commit_file(repo: str, branch: str, path: str, content: str, message: str):
    r = get_repo(repo)
    try:
        existing = r.get_contents(path, ref=branch)
        r.update_file(path, message, content, existing.sha, branch=branch)
    except GithubException:
        r.create_file(path, message, content, branch=branch)


def create_pr(repo: str, branch: str, title: str, body: str) -> str:
    r = get_repo(repo)
    pr = r.create_pull(title=title, body=body, head=branch, base=r.default_branch)
    return pr.html_url


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
