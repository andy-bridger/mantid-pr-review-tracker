#!/usr/bin/env python3

"""Post mantid pull requests that are ready for review to Slack, and follow each one in a thread.

All state lives in Slack: each parent message carries metadata with the PR number, the last green
head and the thread's status, so a sweep can run from nothing. See README.md for the behaviour and setup.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass

TRACKED_REPOSITORY = os.environ.get("TRACKED_REPOSITORY", "mantidproject/mantid")
METADATA_EVENT = "mantid_pr_review"

# The rollup alone can read SUCCESS before Jenkins has posted its first status, so these must
# also be present
DEFAULT_REQUIRED_CONTEXTS = (
    "build-and-test (Linux, linux-64-ci)",
    "build-and-test (Windows, win-64-ci)",
    "pre-commit.ci - pr",
    "Conda: osx",
)
REQUIRED_CONTEXTS = tuple(filter(None, os.environ.get("REQUIRED_CONTEXTS", ",".join(DEFAULT_REQUIRED_CONTEXTS)).split(",")))

ACTIVE, ASSIGNED, DRAFT = "needs_review", "assigned", "draft"

MAX_LISTED_FILES = 15
MAX_LISTED_COMMITS = 10
# A section block's text caps at 3000 characters
SECTION_TEXT_LIMIT = 2800

PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft additions deletions changedFiles baseRefName
        author { login }
        assignees(first: 10) { nodes { login } }
        labels(first: 20) { nodes { name } }
        commits(last: 1) { nodes { commit { oid statusCheckRollup { state
          contexts(first: 100) { nodes { ... on CheckRun { name } ... on StatusContext { context } } } } } } }
      }
    }
  }
}"""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    author: str
    base: str
    is_draft: bool
    assignees: tuple[str, ...]
    labels: tuple[str, ...]
    head_sha: str
    ci_green: bool
    additions: int
    deletions: int
    changed_files: int
    # the rollup's own state, kept for the sweep summary; None when GitHub returned no rollup
    ci_state: str | None = None


@dataclass(frozen=True)
class Thread:
    ts: str
    pr: int
    sha: str
    status: str


class SlackError(RuntimeError):
    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method}: {error}")
        self.error = error


def ci_green(rollup: dict | None) -> bool:
    if not rollup or rollup["state"] != "SUCCESS":
        return False
    names = {node.get("name") or node.get("context") for node in rollup["contexts"]["nodes"]}
    return all(required in names for required in REQUIRED_CONTEXTS)


def decide(pr: PullRequest, thread: Thread | None) -> str | None:
    """The action for an open PR this sweep, which is also its thread's new status. None means stay quiet."""
    if not pr.is_draft and not pr.assignees and pr.ci_green:
        # first post, back in the queue, or a new green head
        return None if thread and thread.status == ACTIVE and thread.sha == pr.head_sha else ACTIVE
    if thread is None or thread.status != ACTIVE:
        return None
    if pr.assignees:
        return ASSIGNED
    if pr.is_draft:
        return DRAFT
    return None  # red or pending CI: only green pushes are posted


def _request(url: str, *, headers: dict, data: bytes | None = None) -> dict | list:
    request = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Slack rate limits a burst of posts, e.g. the first sweep
            if error.code != 429 or attempt == 2:
                raise
            time.sleep(int(error.headers.get("Retry-After", "1")))


def _pull_request(node: dict) -> PullRequest:
    commit = node["commits"]["nodes"][0]["commit"]
    return PullRequest(
        number=node["number"],
        title=node["title"],
        url=node["url"],
        # a deleted account comes back as null
        author=(node["author"] or {}).get("login", "ghost"),
        base=node["baseRefName"],
        is_draft=node["isDraft"],
        assignees=tuple(assignee["login"] for assignee in node["assignees"]["nodes"]),
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        head_sha=commit["oid"],
        ci_green=ci_green(commit["statusCheckRollup"]),
        additions=node["additions"],
        deletions=node["deletions"],
        changed_files=node["changedFiles"],
        ci_state=(commit["statusCheckRollup"] or {}).get("state"),
    )


class GitHub:
    API = "https://api.github.com"

    def __init__(self, token: str, repository: str = TRACKED_REPOSITORY):
        self.repository = repository
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "mantid-pr-review-tracker",
        }

    def _get(self, path: str, **params) -> dict | list:
        return _request(f"{self.API}/repos/{self.repository}/{path}?{urllib.parse.urlencode(params)}", headers=self._headers)

    def open_pull_requests(self) -> list[PullRequest]:
        """Every open PR. Raises rather than return a partial list, which would delete live threads."""
        owner, name = self.repository.split("/")
        prs, cursor = [], None
        while True:
            body = json.dumps({"query": PR_QUERY, "variables": {"owner": owner, "name": name, "cursor": cursor}}).encode()
            response = _request(f"{self.API}/graphql", headers=self._headers, data=body)
            if response.get("errors"):
                raise RuntimeError(f"GraphQL: {response['errors']}")
            page = response["data"]["repository"]["pullRequests"]
            prs.extend(_pull_request(node) for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return prs
            cursor = page["pageInfo"]["endCursor"]

    def files(self, number: int) -> list[dict]:
        files = []
        # the endpoint stops at 3000 files
        for page in range(1, 31):
            batch = self._get(f"pulls/{number}/files", per_page=100, page=page)
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    def compare(self, base: str, head: str) -> dict | None:
        try:
            return self._get(f"compare/{base}...{head}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise


class Slack:
    API = "https://slack.com/api"

    def __init__(self, token: str, channel: str, dry_run: bool = False):
        self.channel = channel
        self.dry_run = dry_run
        self._token = token

    def _call(self, method: str, *, form: bool = False, **params) -> dict:
        # Slack's read methods don't accept JSON bodies
        if form:
            data, content_type = urllib.parse.urlencode(params).encode(), "application/x-www-form-urlencoded"
        else:
            data, content_type = json.dumps(params).encode(), "application/json; charset=utf-8"
        response = _request(
            f"{self.API}/{method}", headers={"Authorization": f"Bearer {self._token}", "Content-Type": content_type}, data=data
        )
        if not response.get("ok"):
            raise SlackError(method, response.get("error", "unknown error"))
        return response

    def _write(self, method: str, **params) -> None:
        if self.dry_run:
            print(f"[dry run] {method}: {json.dumps(params)}")
        else:
            self._call(method, **params)

    def _messages(self, method: str, **params) -> list[dict]:
        messages, cursor = [], None
        while True:
            response = self._call(method, form=True, channel=self.channel, limit=200, **params, **({"cursor": cursor} if cursor else {}))
            messages.extend(response["messages"])
            if not (cursor := response.get("response_metadata", {}).get("next_cursor")):
                return messages

    def threads(self) -> dict[int, Thread]:
        """Every tracked PR's parent message. Newest wins if one was ever duplicated."""
        threads = {}
        # history comes newest first
        for message in self._messages("conversations.history", include_all_metadata="true"):
            metadata = message.get("metadata", {})
            if metadata.get("event_type") == METADATA_EVENT:
                payload = metadata["event_payload"]
                threads.setdefault(int(payload["pr"]), Thread(message["ts"], int(payload["pr"]), payload["sha"], payload["status"]))
        return threads

    def post(self, blocks: list, text: str, thread_ts: str | None = None, metadata: dict | None = None) -> None:
        params = {"channel": self.channel, "blocks": blocks, "text": text, "unfurl_links": False}
        if thread_ts:
            params["thread_ts"] = thread_ts
        if metadata:
            params["metadata"] = metadata
        self._write("chat.postMessage", **params)

    def update(self, ts: str, blocks: list, text: str, metadata: dict) -> None:
        self._write("chat.update", channel=self.channel, ts=ts, blocks=blocks, text=text, metadata=metadata)

    def delete_thread(self, ts: str) -> None:
        """Replies first, so a half-finished delete leaves the parent and its metadata to retry from."""
        # the parent is the oldest message, so it goes last
        for message_ts in sorted({message["ts"] for message in self._messages("conversations.replies", ts=ts)}, key=float, reverse=True):
            try:
                self._write("chat.delete", channel=self.channel, ts=message_ts)
            except SlackError as error:
                # a person's reply can't be deleted by the bot, and must not block the parent
                if error.error != "cant_delete_message" or message_ts == ts:
                    raise


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict:
    if len(text) > SECTION_TEXT_LIMIT:
        text = text[: SECTION_TEXT_LIMIT - 1] + "…"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def diffstat(files: list[dict]) -> str:
    """One line per file, largest change first."""
    files = sorted(files, key=lambda file: file["additions"] + file["deletions"], reverse=True)
    lines = [f"`{_escape(file['filename'])}` +{file['additions']} −{file['deletions']}" for file in files[:MAX_LISTED_FILES]]
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"…and {len(files) - MAX_LISTED_FILES} more files")
    return "\n".join(lines)


def _status_line(pr: PullRequest, status: str) -> str:
    if status == ASSIGNED:
        return f":bust_in_silhouette: *Assigned to* {', '.join(pr.assignees)}"
    if status == DRAFT:
        return ":construction: *Back to draft*"
    return f":large_green_circle: *Needs review* — CI green at `{pr.head_sha[:7]}`"


def _fallback(pr: PullRequest) -> str:
    """The notification preview and screen reader text."""
    return f"#{pr.number} {pr.title}"


def _parent_blocks(github: GitHub, pr: PullRequest, status: str) -> list[dict]:
    labels = "".join(f" `{_escape(label)}`" for label in pr.labels)
    heading = f"<{pr.url}|#{pr.number} {_escape(pr.title)}>\nby `{pr.author}` → `{pr.base}`" + (f" ·{labels}" if labels else "")
    return [
        _section(heading),
        _section(_status_line(pr, status)),
        _section(
            f"*{pr.changed_files} file{'s' * (pr.changed_files != 1)}* +{pr.additions} −{pr.deletions}\n{diffstat(github.files(pr.number))}"
        ),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{pr.url}/files|Files changed>"}]},
    ]


def _changes_since(github: GitHub, pr: PullRequest, sha: str) -> list[str]:
    comparison = github.compare(sha, pr.head_sha)
    # after a rebase, force-push or merge from the base branch the comparison is more than this PR's changes
    if comparison is None or comparison["status"] != "ahead" or any(len(commit["parents"]) > 1 for commit in comparison["commits"]):
        return [":warning: History was rewritten or merged — whole PR shown", diffstat(github.files(pr.number))]

    commits = comparison["commits"]
    lines = [
        f"• `{commit['sha'][:7]}` {_escape(commit['commit']['message'].partition('\n')[0])}" for commit in commits[-MAX_LISTED_COMMITS:]
    ]
    if len(commits) > MAX_LISTED_COMMITS:
        lines.insert(0, f"…{len(commits) - MAX_LISTED_COMMITS} earlier commits")
    return ["\n".join(lines), diffstat(comparison["files"])]


def _reply_blocks(github: GitHub, pr: PullRequest, thread: Thread, status: str) -> list[dict]:
    if status != ACTIVE:
        return [_section(_status_line(pr, status))]
    heading = (
        ":arrows_counterclockwise: *New commits pass CI*"
        if thread.status == ACTIVE
        else ":leftwards_arrow_with_hook: *Back in the review queue*"
    )
    heading += f" at `{pr.head_sha[:7]}`"
    if pr.head_sha == thread.sha:
        return [_section(heading)]
    return [_section(heading), *(_section(text) for text in _changes_since(github, pr, thread.sha))]


def _apply(github: GitHub, slack: Slack, pr: PullRequest, thread: Thread | None, action: str) -> None:
    print(f"#{pr.number}: {action}")
    metadata = {"event_type": METADATA_EVENT, "event_payload": {"pr": pr.number, "sha": pr.head_sha, "status": action}}
    if thread is None:
        slack.post(_parent_blocks(github, pr, action), _fallback(pr), metadata=metadata)
        return
    # parent first: a failed reply loses one update, a failed parent edit would repost it next sweep
    slack.update(thread.ts, _parent_blocks(github, pr, action), _fallback(pr), metadata)
    slack.post(_reply_blocks(github, pr, thread, action), _fallback(pr), thread_ts=thread.ts)


def _attempt(number: int, write, *args) -> int:
    """One bad PR must not stop the rest of the sweep."""
    try:
        write(*args)
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"::warning::#{number}: {error}")
        return 1


def sweep(github: GitHub, slack: Slack) -> int:
    threads = slack.threads()
    prs = {pr.number: pr for pr in github.open_pull_requests()}
    states = Counter(pr.ci_state for pr in prs.values())
    print(f"{len(prs)} open PRs, {sum(pr.ci_green for pr in prs.values())} green, {len(threads)} tracked; CI states {dict(states)}")
    # a token that can't read the tracked repo's checks gets no rollup at all, rather than an error
    if prs and states[None] == len(prs):
        print("::warning::No open PR has a visible CI status - the GitHub token probably can't read the tracked repo's checks")
    gone = threads.keys() - prs.keys()
    if gone and not prs:
        print("::warning::No open PRs returned but threads exist - not deleting anything")
        gone = set()

    failures = 0
    for number in sorted(gone):
        print(f"#{number}: closed")
        failures += _attempt(number, slack.delete_thread, threads[number].ts)
    for pr in sorted(prs.values(), key=lambda pr: pr.number):
        thread = threads.get(pr.number)
        if (action := decide(pr, thread)) is not None:
            failures += _attempt(pr.number, _apply, github, slack, pr, thread, action)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="read GitHub and Slack, log every Slack write and send none")
    args = parser.parse_args(argv)

    github = GitHub(os.environ["GITHUB_TOKEN"])
    slack = Slack(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_CHANNEL_ID"], args.dry_run)
    return sweep(github, slack)


if __name__ == "__main__":
    raise SystemExit(main())
