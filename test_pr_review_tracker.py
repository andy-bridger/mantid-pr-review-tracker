import dataclasses
import unittest
from unittest import mock

import pr_review_tracker as tracker
from pr_review_tracker import ACTIVE, ASSIGNED, DRAFT, METADATA_EVENT, REQUIRED_CONTEXTS, PullRequest, Slack, SlackError, Thread

PR = PullRequest(1, "t", "https://github.com/o/r/pull/1", "alice", "main", False, (), (), "a" * 40, True, 3, 1, 1)
THREAD = Thread("1.0", 1, "a" * 40, ACTIVE)


def make_pr(**overrides) -> PullRequest:
    return dataclasses.replace(PR, **overrides)


def make_thread(**overrides) -> Thread:
    return dataclasses.replace(THREAD, **overrides)


def commit(sha: str, message: str, parents: int = 1) -> dict:
    return {"sha": sha * 40, "commit": {"message": message}, "parents": [{}] * parents}


def messages(*items) -> dict:
    return {"ok": True, "messages": list(items)}


def parent(ts: str, pr: int, event_type: str = METADATA_EVENT) -> dict:
    return {"ts": ts, "metadata": {"event_type": event_type, "event_payload": {"pr": pr, "sha": "a" * 40, "status": ACTIVE}}}


class FakeGitHub:
    def __init__(self, prs=(), comparison=None, failing=()):
        self.prs, self.comparison, self.failing = list(prs), comparison, failing

    def open_pull_requests(self):
        return self.prs

    def files(self, number):
        if number in self.failing:
            raise RuntimeError("boom")
        return [{"filename": "whole", "additions": 1, "deletions": 1}]

    def compare(self, base, head):
        return self.comparison


class FakeSlack:
    def __init__(self, threads=None):
        self._threads = threads or {}
        self.calls = []

    def threads(self):
        return self._threads

    def post(self, blocks, text, thread_ts=None, metadata=None):
        self.calls.append(("reply" if thread_ts else "post", text))

    def update(self, ts, blocks, text, metadata):
        self.calls.append(("update", text))

    def delete_thread(self, ts):
        self.calls.append(("delete", ts))


class DecideTest(unittest.TestCase):
    def test_new_pr_is_posted_only_when_eligible(self):
        self.assertEqual(tracker.decide(make_pr(), None), ACTIVE)
        for pr in (make_pr(is_draft=True), make_pr(assignees=("bob",)), make_pr(ci_green=False)):
            self.assertIsNone(tracker.decide(pr, None))

    def test_active_thread_posts_only_a_new_green_head(self):
        self.assertIsNone(tracker.decide(make_pr(), make_thread()))
        self.assertEqual(tracker.decide(make_pr(head_sha="b" * 40), make_thread()), ACTIVE)
        self.assertIsNone(tracker.decide(make_pr(head_sha="b" * 40, ci_green=False), make_thread()))

    def test_active_thread_pauses_on_assignment_or_draft(self):
        self.assertEqual(tracker.decide(make_pr(assignees=("bob",)), make_thread()), ASSIGNED)
        self.assertEqual(tracker.decide(make_pr(is_draft=True), make_thread()), DRAFT)

    def test_paused_thread_resumes_only_once_eligible_again(self):
        for status in (ASSIGNED, DRAFT):
            self.assertEqual(tracker.decide(make_pr(), make_thread(status=status)), ACTIVE)
            self.assertIsNone(tracker.decide(make_pr(assignees=("bob",), is_draft=True), make_thread(status=status)))


class CiGreenTest(unittest.TestCase):
    def test_needs_success_and_every_required_context(self):
        def rollup(state="SUCCESS", names=REQUIRED_CONTEXTS):
            return {"state": state, "contexts": {"nodes": [{"name": name} for name in names]}}

        self.assertTrue(tracker.ci_green(rollup()))
        self.assertFalse(tracker.ci_green(rollup("PENDING")))
        self.assertFalse(tracker.ci_green(rollup(names=REQUIRED_CONTEXTS[1:])))
        self.assertFalse(tracker.ci_green(None))

    def test_status_contexts_count_by_context(self):
        self.assertTrue(tracker.ci_green({"state": "SUCCESS", "contexts": {"nodes": [{"context": name} for name in REQUIRED_CONTEXTS]}}))


class MessageTest(unittest.TestCase):
    def test_diffstat_lists_the_largest_changes_first_and_truncates(self):
        lines = tracker.diffstat([{"filename": f"f{i}", "additions": i, "deletions": 0} for i in range(20)]).splitlines()
        self.assertEqual(lines[0], "`f19` +19 −0")
        self.assertEqual(lines[-1], "…and 5 more files")
        self.assertEqual(len(lines), tracker.MAX_LISTED_FILES + 1)

    def test_section_is_capped(self):
        self.assertEqual(len(tracker._section("x" * 5000)["text"]["text"]), tracker.SECTION_TEXT_LIMIT)

    def test_parent_escapes_the_title(self):
        blocks = tracker._parent_blocks(FakeGitHub(), make_pr(title="a <b> & c"), ACTIVE)
        self.assertIn("a &lt;b&gt; &amp; c", blocks[0]["text"]["text"])

    def test_changes_since_lists_new_commits_and_their_diffstat(self):
        comparison = {
            "status": "ahead",
            "commits": [commit("b", "Fix it\n\nbody")],
            "files": [{"filename": "x", "additions": 1, "deletions": 2}],
        }
        self.assertEqual(
            tracker._changes_since(FakeGitHub(comparison=comparison), make_pr(), "a" * 40), ["• `bbbbbbb` Fix it", "`x` +1 −2"]
        )

    def test_changes_since_shows_the_whole_pr_when_history_was_rewritten(self):
        for comparison in (
            None,
            {"status": "diverged", "commits": [], "files": []},
            {"status": "ahead", "commits": [commit("b", "Merge main", parents=2)], "files": []},
        ):
            texts = tracker._changes_since(FakeGitHub(comparison=comparison), make_pr(), "a" * 40)
            self.assertEqual(texts[1], "`whole` +1 −1")


class SlackTest(unittest.TestCase):
    def setUp(self):
        self.slack = Slack("token", "C1")

    def test_threads_keep_only_ours_and_the_newest_per_pr(self):
        history = messages(parent("3.0", 7), {"ts": "2.5", "text": "chatter"}, parent("2.0", 8, "other"), parent("1.0", 7))
        with mock.patch.object(self.slack, "_call", return_value=history):
            self.assertEqual(self.slack.threads(), {7: Thread("3.0", 7, "a" * 40, ACTIVE)})

    def test_delete_thread_deletes_replies_before_the_parent(self):
        with mock.patch.object(self.slack, "_call", return_value=messages({"ts": "1.0"}, {"ts": "2.0"}, {"ts": "10.0"})) as call:
            self.slack.delete_thread("1.0")
        self.assertEqual([c.kwargs["ts"] for c in call.call_args_list if c.args[0] == "chat.delete"], ["10.0", "2.0", "1.0"])

    def test_delete_thread_skips_replies_it_cannot_delete(self):
        def call(method, **params):
            if method == "chat.delete" and params["ts"] == "2.0":
                raise SlackError(method, "cant_delete_message")
            return messages({"ts": "1.0"}, {"ts": "2.0"})

        with mock.patch.object(self.slack, "_call", side_effect=call) as patched:
            self.slack.delete_thread("1.0")
        self.assertEqual(patched.call_args_list[-1], mock.call("chat.delete", channel="C1", ts="1.0"))

    def test_dry_run_reads_but_never_writes(self):
        slack = Slack("token", "C1", dry_run=True)
        with mock.patch.object(slack, "_call", return_value=messages({"ts": "1.0"})) as call, mock.patch("builtins.print"):
            slack.post([], "text")
            slack.update("1.0", [], "text", {})
            slack.delete_thread("1.0")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["conversations.replies"])


@mock.patch("builtins.print")
class SweepTest(unittest.TestCase):
    def test_closed_pr_thread_is_deleted(self, _):
        slack = FakeSlack({5: make_thread(ts="5.0", pr=5)})
        self.assertEqual(tracker.sweep(FakeGitHub([make_pr(ci_green=False)]), slack), 0)
        self.assertEqual(slack.calls, [("delete", "5.0")])

    def test_warns_when_no_ci_status_is_visible(self, print_):
        tracker.sweep(FakeGitHub([make_pr(ci_green=False), make_pr(number=2, ci_green=False)]), FakeSlack())
        self.assertIn("::warning::No open PR has a visible CI status", print_.call_args_list[-1].args[0])

    def test_no_open_prs_deletes_nothing(self, _):
        slack = FakeSlack({5: make_thread(ts="5.0", pr=5)})
        tracker.sweep(FakeGitHub(), slack)
        self.assertEqual(slack.calls, [])

    def test_parent_is_updated_before_the_reply(self, _):
        slack = FakeSlack({1: make_thread()})
        tracker.sweep(FakeGitHub([make_pr(head_sha="b" * 40)], comparison={"status": "ahead", "commits": [], "files": []}), slack)
        self.assertEqual(slack.calls, [("update", "#1 t"), ("reply", "#1 t")])

    def test_one_failing_pr_does_not_stop_the_rest(self, _):
        slack = FakeSlack()
        self.assertEqual(tracker.sweep(FakeGitHub([make_pr(number=1), make_pr(number=2)], failing={1}), slack), 1)
        self.assertEqual(slack.calls, [("post", "#2 t")])


if __name__ == "__main__":
    unittest.main()
