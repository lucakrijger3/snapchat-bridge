First of all, yes this is vibecoded. Do I care? No. So why would you.. I wanted Snapchat in my Beeper client, so with my little coding skills and my prompting skills I let Copilot make it.
It works. If anyone is willing to take his time and make these approximately 10000 lines of codes safe and optimized, be my guest and clone this repositry. Also, I'm Dutch so this bridge will probably only work if your snapchat's language is Dutch.

# Snapchat Bridge

## What works

- [x] Discover Snapchat chats and create corresponding Beeper chats.
- [x] Sync messages, including messages sent directly in Snapchat.
- [x] Show incoming reactions in Beeper and send supported emoji reactions from Beeper.
- [x] Reply to and delete text messages.
- [x] Send custom best-friend status and streak-at-risk notifications. Streak counts are tracked and available with `!streak` and `!info`; positive streak-count change notifications are intentionally disabled.
- [x] Download incoming media and send supported images from Beeper.
- [x] Notify about received Snaps with a link to Snapchat, and remove the notice when the Snap is opened.
- [x] Sync incoming voice messages, show “Nieuw audiobericht,” and mark the latest Beeper message as read when Snapchat shows “Opened” or “Geopend.”
- [x] Refresh and sync Snapchat avatars.
- [x] Use `!streak` or `!info` for saved streak/friend status, and `!snap` for an “Open Snapchat” link in a bridged chat.
- [x] Keep text replies and reactions available while rejecting replies, reactions, and deletions on media attachments with a temporary notice.
- Chats with one remote sender are registered as direct chats; chats with multiple senders are registered as group chats.
- Beeper permits one reaction per user per message; a new reaction replaces that user's previous one.
- Media interaction attempts are removed and produce a temporary explanatory notice. Large incoming media groups are split into Beeper messages of at most 10 attachments. Outgoing images are limited to 40 MiB; outgoing videos, stickers, and audio are not supported.
- Command messages are removed immediately, and command responses expire after 15 seconds.
- Consecutive identical plain-text messages from the same sender are displayed once when their content matches after Unicode and whitespace normalization. Messages separated by another message, with different reply targets, or containing media remain distinct.
- Startup sync omits historical media. Later targeted scans deliver only media newer than the initial-sync cutoff. New media found on an already-synchronized message is sent in a follow-up update.
- The bridge uses Playwright with the visible Snapchat Web page and does not call Snapchat's private APIs. Snapchat may change its page structure; if that happens, the browser automation may need updated selectors.

## Snapchat language

The bridge is built and tested primarily with Snapchat set to Dutch. Some common English status labels are recognized, but the English interface and other languages are not fully supported; controls such as **Chat sluiten** and **Afgeleverd** are matched using Dutch text.

To adapt it to another language, search `app.py` for Dutch interface strings (including `Chat sluiten`, `Afgeleverd`, and `Aan het typen...`) and add that language's exact labels to the relevant selectors and status-matching patterns. Alternatively, set Snapchat's display language to Dutch.

## Requirements

- Python 3.10 or later
- Google Chrome
- Go
- `bbctl`, installed and authenticated as described in the [bridge-manager README](https://github.com/beeper/bridge-manager#usage)

Install the Python dependency:

```sh
python3 -m pip install -r requirements.txt
```

## Start the bridge

1. Before the first bridge sync, open your Snapchat chats and Snaps in the browser window so they can be discovered.
2. Run the launcher:

   ```sh
   ./run_beeper_bridge.sh
   ```

   The launcher starts the Snapchat browser automation and Go bridge. If `sh-snapchat` is not registered, it generates the bridge configuration, registers the bridge, and then starts it. Keep the launcher running.
3. If automatic login is not available, open a direct chat with the bridge bot in Beeper and send:

   ```text
   !snapchat login local
   ```

   The bot checks the existing Snapchat sign-in, creates chats for enabled conversations, and synchronizes saved text history.

The generated `data/beeper-bridge.yaml` contains credentials; keep it private and do not share or commit it. The optional Go bridge uses mautrix bridgev2. On macOS without libolm, build and run with `-tags nocrypto`; this disables Matrix end-to-end encryption, so bridge encryption must also be disabled.

## Reset the bridge

Stop the bridge, then run:

```sh
python3 reset_database.py --confirm
```

This removes the `sh-snapchat` Beeper registration, backs up and clears the local chat database, and removes log entries older than 24 hours. `bbctl` prompts for confirmation. Start the bridge again with `./run_beeper_bridge.sh`.
