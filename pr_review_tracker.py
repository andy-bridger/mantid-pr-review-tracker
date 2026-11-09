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
# semicolon-separated, because the check names themselves contain commas
REQUIRED_CONTEXTS = tuple(filter(None, os.environ.get("REQUIRED_CONTEXTS", "").split(";"))) or DEFAULT_REQUIRED_CONTEXTS

ACTIVE, ASSIGNED, DRAFT = "needs_review", "assigned", "draft"

MAX_LISTED_FILES = 15
# the attachment bar: Slack's green while a PR needs review, grey while it is paused
ACTIVE_COLOUR, PAUSED_COLOUR = "#2eb67d", "#9e9ea6"
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
    # kept for the sweep log; ci_state is None when GitHub returned no rollup
    ci_state: str | None = None
    ci_contexts: frozenset[str] = frozenset()


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


def context_names(rollup: dict | None) -> frozenset[str]:
    # a context the token can't see comes back as an empty node
    return frozenset(node.get("name") or node.get("context") or "<hidden>" for node in (rollup or {}).get("contexts", {}).get("nodes", []))


def ci_green(rollup: dict | None) -> bool:
    if not rollup or rollup["state"] != "SUCCESS":
        return False
    return context_names(rollup).issuperset(REQUIRED_CONTEXTS)


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
        ci_contexts=context_names(commit["statusCheckRollup"]),
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

    def post(self, message: dict, text: str, thread_ts: str | None = None, metadata: dict | None = None) -> None:
        """message holds the blocks and, optionally, attachments."""
        params = {"channel": self.channel, **message, "text": text, "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            params["thread_ts"] = thread_ts
        if metadata:
            params["metadata"] = metadata
        self._write("chat.postMessage", **params)

    def update(self, ts: str, message: dict, text: str, metadata: dict) -> None:
        self._write("chat.update", channel=self.channel, ts=ts, **message, text=text, metadata=metadata)

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
    """A monospace table of the files, largest change first."""
    if not files:
        return ""
    files = sorted(files, key=lambda file: file["additions"] + file["deletions"], reverse=True)
    shown = files[:MAX_LISTED_FILES]
    added, deleted = [f"+{file['additions']}" for file in shown], [f"−{file['deletions']}" for file in shown]
    added_width, deleted_width = max(map(len, added)), max(map(len, deleted))
    lines = [f"{a:>{added_width}} {d:>{deleted_width}}  {_escape(file['filename'])}" for a, d, file in zip(added, deleted, shown)]
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"…and {len(files) - MAX_LISTED_FILES} more files")
    return "```\n" + "\n".join(lines) + "\n```"


def _files_title(count: int) -> str:
    return f"{count} file{'s' * (count != 1)} changed"


def _files_attachment(title: str, files: list[dict], colour: str, title_link: str | None = None) -> dict:
    """Slack folds a long attachment behind "Show more", which no Block Kit block can do."""
    attachment = {"color": colour, "title": title, "text": diffstat(files), "mrkdwn_in": ["text"], "fallback": title}
    if title_link:
        attachment["title_link"] = title_link
    return attachment


def _status_line(pr: PullRequest, status: str) -> str:
    return f":bust_in_silhouette: Assigned to {_escape(', '.join(pr.assignees))}" if status == ASSIGNED else ":construction: Back to draft"


def _fallback(pr: PullRequest) -> str:
    """The notification preview and screen reader text."""
    return f"#{pr.number} {_escape(pr.title)}"


def _parent_message(github: GitHub, pr: PullRequest, status: str) -> dict:
    headline = f"*#{pr.number}  ·  +{pr.additions} −{pr.deletions}  ·  <{pr.url}|{_escape(pr.title)}>*"
    details = [f"{_escape(pr.author)} → {_escape(pr.base)}", *map(_escape, pr.labels)]
    if status != ACTIVE:
        details.insert(0, _status_line(pr, status))
    return {
        "blocks": [_section(headline), {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(details)}]}],
        "attachments": [
            _files_attachment(
                _files_title(pr.changed_files),
                github.files(pr.number),
                ACTIVE_COLOUR if status == ACTIVE else PAUSED_COLOUR,
                f"{pr.url}/files",
            )
        ],
    }


def _changes_since(github: GitHub, pr: PullRequest, sha: str) -> tuple[str, str, list[dict]]:
    """The summary line, the files attachment's title, and the files changed since sha."""
    comparison = github.compare(sha, pr.head_sha)
    # after a rebase, force-push or merge from the base branch the comparison is more than this PR's changes
    if comparison is None or comparison["status"] != "ahead" or any(len(commit["parents"]) > 1 for commit in comparison["commits"]):
        return ":warning: History was rewritten or merged, so this is the whole PR", _files_title(pr.changed_files), github.files(pr.number)

    commits = comparison["commits"]
    lines = [
        f"• `{commit['sha'][:7]}` {_escape(commit['commit']['message'].partition('\n')[0])}" for commit in commits[-MAX_LISTED_COMMITS:]
    ]
    if len(commits) > MAX_LISTED_COMMITS:
        lines.insert(0, f"…{len(commits) - MAX_LISTED_COMMITS} earlier commits")
    return "\n".join(lines), f"{_files_title(len(comparison['files']))} since {sha[:7]}", comparison["files"]


def _reply_message(github: GitHub, pr: PullRequest, thread: Thread, status: str) -> dict:
    if status != ACTIVE:
        return {"blocks": [_section(_status_line(pr, status))]}
    heading = (
        ":arrows_counterclockwise: *New commits pass CI*"
        if thread.status == ACTIVE
        else ":leftwards_arrow_with_hook: *Back in the review queue*"
    )
    if pr.head_sha == thread.sha:
        return {"blocks": [_section(heading)]}
    summary, title, files = _changes_since(github, pr, thread.sha)
    added, deleted = sum(file["additions"] for file in files), sum(file["deletions"] for file in files)
    return {
        "blocks": [_section(f"{heading}  ·  +{added} −{deleted}\n{summary}")],
        "attachments": [_files_attachment(title, files, ACTIVE_COLOUR)],
    }


def _apply(github: GitHub, slack: Slack, pr: PullRequest, thread: Thread | None, action: str) -> None:
    print(f"#{pr.number}: {action}")
    metadata = {"event_type": METADATA_EVENT, "event_payload": {"pr": pr.number, "sha": pr.head_sha, "status": action}}
    if thread is None:
        slack.post(_parent_message(github, pr, action), _fallback(pr), metadata=metadata)
        return
    # parent first: a failed reply loses one update, a failed parent edit would repost it next sweep
    slack.update(thread.ts, _parent_message(github, pr, action), _fallback(pr), metadata)
    slack.post(_reply_message(github, pr, thread, action), _fallback(pr), thread_ts=thread.ts)


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
    for pr in prs.values():
        if pr.ci_state == "SUCCESS" and not pr.ci_green and not pr.is_draft and not pr.assignees:
            missing = sorted(set(REQUIRED_CONTEXTS) - pr.ci_contexts)
            print(f"#{pr.number}: CI is SUCCESS but {missing} not reported; saw {sorted(pr.ci_contexts)}")
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
