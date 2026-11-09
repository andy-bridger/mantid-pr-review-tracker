import dataclasses
import json
import unittest
from unittest import mock

import pr_review_tracker as tracker
from pr_review_tracker import ACTIVE, ASSIGNED, DRAFT, METADATA_EVENT, REMOVED, REQUIRED_CONTEXTS, PullRequest, Slack, SlackError, Thread

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

    def post(self, message, text, thread_ts=None, metadata=None):
        self.calls.append(("reply" if thread_ts else "post", text))

    def update(self, ts, message, text, metadata):
        self.calls.append(("update", text))

    def delete_thread(self, ts, keep_parent=False):
        self.calls.append(("delete replies" if keep_parent else "delete", ts))


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

    def test_reaction_removes_a_post_whatever_its_status(self):
        for status in (ACTIVE, ASSIGNED, DRAFT):
            self.assertEqual(tracker.decide(make_pr(), make_thread(status=status, removed_by="U1")), REMOVED)

    def test_removed_pr_returns_only_on_a_new_green_head(self):
        stub = make_thread(status=REMOVED, removed_by="U1")
        self.assertIsNone(tracker.decide(make_pr(), stub))
        self.assertIsNone(tracker.decide(make_pr(head_sha="b" * 40, ci_green=False), stub))
        self.assertEqual(tracker.decide(make_pr(head_sha="b" * 40), stub), ACTIVE)


class CiGreenTest(unittest.TestCase):
    def test_default_required_contexts_survive_parsing(self):
        # the names contain commas, which once split them into fragments that never matched
        self.assertEqual(REQUIRED_CONTEXTS, tracker.DEFAULT_REQUIRED_CONTEXTS)

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
    def test_diffstat_is_an_aligned_table_of_the_largest_changes_first(self):
        lines = tracker.diffstat([{"filename": f"f{i}", "additions": i, "deletions": 0} for i in range(20)]).splitlines()
        self.assertEqual(lines[:2], ["```", "+19 −0  f19"])
        self.assertIn(" +9 −0  f9", lines)
        self.assertEqual(lines[-2:], ["…and 5 more files", "```"])
        self.assertEqual(len(lines), tracker.MAX_LISTED_FILES + 3)
        self.assertEqual(tracker.diffstat([]), "")

    def test_section_is_capped(self):
        self.assertEqual(len(tracker._section("x" * 5000)["text"]["text"]), tracker.SECTION_TEXT_LIMIT)

    def test_pr_text_cannot_mention_or_link(self):
        # anyone can open a mantid PR, so its text must not reach Slack as <!channel>, <@U…> or <url|…>
        pr = make_pr(title="<!channel> & <https://evil|x>", labels=("<!here>",), assignees=("<@U1>",))
        rendered = json.dumps(tracker._parent_message(FakeGitHub(), pr, ASSIGNED)) + tracker._fallback(pr)
        for control in ("<!", "<@", "<https://evil"):
            self.assertNotIn(control, rendered)

    def test_paused_parent_says_why_and_greys_out(self):
        message = tracker._parent_message(FakeGitHub(), make_pr(assignees=("bob",)), ASSIGNED)
        self.assertIn("Assigned to bob", message["blocks"][1]["elements"][0]["text"])
        self.assertEqual(message["attachments"][0]["color"], tracker.PAUSED_COLOUR)

    def test_changes_since_lists_new_commits_and_their_diffstat(self):
        comparison = {
            "status": "ahead",
            "commits": [commit("b", "Fix it\n\nbody")],
            "files": [{"filename": "x", "additions": 1, "deletions": 2}],
        }
        self.assertEqual(
            tracker._changes_since(FakeGitHub(comparison=comparison), make_pr(), "a" * 40),
            ("• `bbbbbbb` Fix it", "1 file changed since aaaaaaa", comparison["files"]),
        )

    def test_changes_since_shows_the_whole_pr_when_history_was_rewritten(self):
        for comparison in (
            None,
            {"status": "diverged", "commits": [], "files": []},
            {"status": "ahead", "commits": [commit("b", "Merge main", parents=2)], "files": []},
        ):
            summary, _, files = tracker._changes_since(FakeGitHub(comparison=comparison), make_pr(), "a" * 40)
            self.assertIn("whole PR", summary)
            self.assertEqual(files, FakeGitHub().files(1))

    def test_stub_names_the_remover_and_drops_the_files(self):
        stub = tracker._stub_message(make_pr(), "U1")
        self.assertIn("removed by <@U1>", stub["blocks"][0]["elements"][0]["text"])
        self.assertEqual(stub["attachments"], [])
        self.assertNotIn("<!", json.dumps(tracker._stub_message(make_pr(), "<!channel>")))


class SlackTest(unittest.TestCase):
    def setUp(self):
        self.slack = Slack("token", "C1")

    def test_threads_keep_only_ours_and_the_newest_per_pr(self):
        history = messages(parent("3.0", 7), {"ts": "2.5", "text": "chatter"}, parent("2.0", 8, "other"), parent("1.0", 7))
        with mock.patch.object(self.slack, "_call", return_value=history):
            self.assertEqual(self.slack.threads(), {7: Thread("3.0", 7, "a" * 40, ACTIVE)})

    def test_threads_note_who_reacted_to_remove(self):
        reactions = [{"name": "+1", "users": ["U9"]}, {"name": tracker.REMOVE_REACTION, "users": ["U1", "U2"]}]
        with mock.patch.object(self.slack, "_call", return_value=messages({**parent("3.0", 7), "reactions": reactions})):
            self.assertEqual(self.slack.threads()[7].removed_by, "U1")

    def test_delete_thread_can_keep_the_parent(self):
        with mock.patch.object(self.slack, "_call", return_value=messages({"ts": "1.0"}, {"ts": "2.0"})) as call:
            self.slack.delete_thread("1.0", keep_parent=True)
        self.assertEqual([c.kwargs["ts"] for c in call.call_args_list if c.args[0] == "chat.delete"], ["2.0"])

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
            slack.post({}, "text")
            slack.update("1.0", {}, "text", {})
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

    def test_removal_deletes_replies_then_shrinks_the_post(self, _):
        slack = FakeSlack({1: make_thread(removed_by="U1")})
        tracker.sweep(FakeGitHub([make_pr()]), slack)
        self.assertEqual(slack.calls, [("delete replies", "1.0"), ("update", "#1 t")])

    def test_returning_pr_replaces_its_stub_with_a_fresh_post(self, _):
        slack = FakeSlack({1: make_thread(status=REMOVED, removed_by="U1")})
        tracker.sweep(FakeGitHub([make_pr(head_sha="b" * 40)]), slack)
        self.assertEqual(slack.calls, [("delete", "1.0"), ("post", "#1 t")])

    def test_reset_deletes_every_post(self, _):
        slack = Slack("token", "C1")

        def call(method, **params):
            if method == "conversations.history":
                return messages(parent("3.0", 7), {"ts": "2.5", "text": "chatter"}, parent("1.0", 7))
            return messages({"ts": params.get("ts")})

        with mock.patch.object(slack, "_call", side_effect=call) as patched:
            self.assertEqual(tracker.reset(slack), 0)
        self.assertEqual([c.kwargs["ts"] for c in patched.call_args_list if c.args[0] == "chat.delete"], ["3.0", "1.0"])


if __name__ == "__main__":
    unittest.main()
