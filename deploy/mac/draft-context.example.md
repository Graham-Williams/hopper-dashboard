# Setup brief for the Inbox drafter (example shape)

<!--
Point INBOX_DRAFT_CONTEXT_FILE (in ~/.config/hopper-dashboard/env) at your own copy of this.
It is sent to Anthropic (Claude) with every voice note, as reference data, so the drafter can
tell which project "the backup thing" or "the wheel page" means. Keep it short (the worker cuts
it at 6000 characters) and put nothing in it you would not send with a note: no secrets, tokens,
passwords, addresses or other people's details. The worker ignores the file if it is group- or
world-writable (0644 or 0600 are fine), and it is read fresh on every run.
-->

## Projects

- <repo>: <one-line purpose; key nouns people use for it>
- <repo>: <one-line purpose; key nouns people use for it>
- <repo>: <one-line purpose; key nouns people use for it>

## Shared infrastructure

- <thing>: <what it is; what it is called in conversation>
- <thing>: <what it is; what it is called in conversation>

## Words that mean a specific project

- "<nickname>" -> <repo>
- "<nickname>" -> <repo>
