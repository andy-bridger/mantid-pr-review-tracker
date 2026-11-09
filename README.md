# Mantid PR review tracker

Posts [mantid](https://github.com/mantidproject/mantid) pull requests that are ready for review to a Slack channel, and follows each one in its message's thread.

- A PR is posted once it is **open, not a draft, has no assignees and its CI is fully green**. The post has the title, author, base branch, labels and a diffstat.
- Each later push that goes green is a thread reply with the new commits and a diffstat since the last green post.
- Assigning the PR, or converting it back to draft, edits the post's status line and pauses the thread. Unassigning it, or marking it ready again, resumes it.
- Merging or closing the PR deletes the thread.

"Fully green" means GitHub's combined status for the head commit is `SUCCESS` and every check in `REQUIRED_CONTEXTS` (see `pr_review_tracker.py`) has reported. Jenkins doesn't build bot PRs, so dependabot PRs never qualify.

The sweep runs every 5 minutes between 09:00 and 17:00 UK time, Monday to Friday. Nothing is stored outside Slack: each post carries metadata with the PR number, the last green commit and its status. Deleting a post by hand makes the tracker forget that PR, and it reposts it on the next sweep if it is still eligible.

## Slack app setup

1. At [api.slack.com/apps](https://api.slack.com/apps): **Create New App → From a manifest**:
   ```yaml
   display_information:
     name: Mantid PR Review
   features:
     bot_user:
       display_name: Mantid PR Review
   oauth_config:
     scopes:
       bot:
         - chat:write        # post, edit and delete its own messages
         - channels:history  # read the channel and threads back; add groups:history for a private channel
   ```
2. Install it to the workspace, then run `/invite @Mantid PR Review` in the channel.
3. In this repo's **Settings → Secrets and variables → Actions**, add:
   - secret `SLACK_BOT_TOKEN`: the app's *Bot User OAuth Token* (`xoxb-…`)
   - variable `SLACK_CHANNEL_ID`: the channel ID (`C0…`, from the channel's *About* tab), not its name

## Running it

```sh
python -m unittest -v test_pr_review_tracker

# --dry-run reads GitHub and Slack and logs every Slack write without sending it
GITHUB_TOKEN=<token> SLACK_BOT_TOKEN=<xoxb-…> SLACK_CHANNEL_ID=<C0…> python pr_review_tracker.py --dry-run
```

`TRACKED_REPOSITORY` (default `mantidproject/mantid`) and `REQUIRED_CONTEXTS` (comma-separated) can be overridden to test against a scratch repo. For example, use `REQUIRED_CONTEXTS=fake-ci` and fake CI with `gh api repos/<you>/<scratch>/statuses/<sha> -f state=success -f context=fake-ci`.

From GitHub, **Actions → Sweep mantid PRs → Run workflow** runs one sweep at any hour. It defaults to a dry run.

The first real sweep posts every PR that already qualifies, so do a dry run first to see how many there are.
