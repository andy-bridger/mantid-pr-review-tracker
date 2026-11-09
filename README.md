# Mantid PR review tracker

Posts [mantid](https://github.com/mantidproject/mantid) pull requests that are ready for review to a Slack channel, and follows each one in its message's thread.

- A PR is posted once it is **open, not a draft, has no assignees and its CI is fully green**. The post shows the PR number, its size and a link, then the author, base branch and labels, then a collapsible list of the changed files.
- Each later push that goes green is a thread reply with the new commits and the files changed since the last green post.
- Assigning the PR, or converting it back to draft, edits the post's status and pauses the thread. Unassigning it, or marking it ready again, resumes it.
- Merging or closing the PR deletes the thread.

"Fully green" means GitHub's combined status for the head commit is `SUCCESS` and every check in `REQUIRED_CONTEXTS` (see `pr_review_tracker.py`) has reported. Jenkins doesn't build bot PRs, so dependabot PRs never qualify.

The sweep runs every 5 minutes between 09:00 and 17:00 UK time, Monday to Friday. Nothing is stored outside Slack: each post carries metadata with the PR number, the last green commit and its status.

## Removing posts

Slack doesn't let members delete an app's messages, so:

- **Anyone in the channel** can react to a post with 🗑️ (`:wastebasket:`). The next sweep deletes the thread's replies and shrinks the post to one line saying who removed it. That line is the tracker's memory: the PR stays quiet until a new commit passes CI. Then the line is deleted and a fresh post appears at the bottom of the channel.
- **Whoever can run this repo's workflows** can wipe the channel: **Actions → Sweep mantid PRs → Run workflow** with *RESET* ticked (and *DRY_RUN* unticked). This deletes every post and thread the bot has made, and the next sweep reposts whatever still qualifies. It's meant for testing.

Replies people write in a thread can't be deleted by the bot, so they stay behind as a "This message was deleted" thread when the post goes.

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
         - chat:write       # post, edit and delete its own messages
         - groups:history   # read back the private channels it has been invited to
   ```
   This assumes a private channel. A public channel needs `channels:history` instead, which lets the token read every public channel's history (see below). If 🗑️ reactions are never picked up, also add `reactions:read`.
2. Install it to the workspace, then run `/invite @Mantid PR Review` in the channel.
3. In this repo's **Settings → Secrets and variables → Actions**, add:
   - secret `SLACK_BOT_TOKEN`: the app's *Bot User OAuth Token* (`xoxb-…`)
   - variable `SLACK_CHANNEL_ID`: the channel ID (`C0…`, from the channel's *About* tab), not its name

## Security

The bot token is the one thing worth stealing. These keep what it can do, and who can reach it, small:

- **Minimal scopes.** `chat:write` can only post where the bot has been invited, and can only edit or delete the bot's own messages. In a private channel, `groups:history` only reads the private channels the bot has been invited to. No public channel, DM or file is readable.
- **No inbound surface.** The app has no event subscriptions, interactivity, slash commands or redirect URLs, so nothing on the internet can call it. Keep it that way, and keep the app undistributed (installed to the mantid workspace only).
- **Untrusted PR text is defused.** Anyone can open a mantid PR. Its title, labels, file names and commit messages are escaped before they reach Slack, so they can't become `@channel`, `@here`, user mentions or disguised links. Link and media previews are off.
- **Nothing third-party sees the token.** The script uses the Python standard library only, and the one action (`actions/checkout`) is pinned to a commit SHA.
- **Secrets aren't exposed to pull requests.** GitHub never passes secrets to workflows run from forks, and the sweep has no `pull_request` trigger.

Repository settings to check:
- **Settings → Actions → General:**
  - *Fork pull request workflows from outside collaborators*: **Require approval for all outside collaborators**
  - *Workflow permissions*: **Read repository contents**
- **Branch protection on `main`:** block force pushes and deletion.
- **Two-factor authentication** on every account with write access, which is what really guards the secret.

If the token ever leaks, go to the Slack app's **OAuth & Permissions**, revoke the token, reinstall the app, and update `SLACK_BOT_TOKEN`.

## Running it

```sh
python -m unittest -v test_pr_review_tracker

# --dry-run reads GitHub and Slack and logs every Slack write without sending it
GITHUB_TOKEN=<token> SLACK_BOT_TOKEN=<xoxb-…> SLACK_CHANNEL_ID=<C0…> python pr_review_tracker.py --dry-run
SLACK_BOT_TOKEN=<xoxb-…> SLACK_CHANNEL_ID=<C0…> python pr_review_tracker.py --reset
```

`TRACKED_REPOSITORY` (default `mantidproject/mantid`) and `REQUIRED_CONTEXTS` (semicolon-separated, because check names contain commas) can be overridden to test against a scratch repo. For example, use `REQUIRED_CONTEXTS=fake-ci` and fake CI with `gh api repos/<you>/<scratch>/statuses/<sha> -f state=success -f context=fake-ci`.

From GitHub, **Actions → Sweep mantid PRs → Run workflow** runs one sweep at any hour. It defaults to a dry run.
