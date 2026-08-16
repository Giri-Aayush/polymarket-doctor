# Recording the demo

Three ways to show what the tool does, cheapest first. The tool's output is
already colorful and self-explanatory, so a terminal recording carries it
without narration.

## Option A — a GIF for the README (recommended)

[VHS](https://github.com/charmbracelet/vhs) renders a terminal session from a
script, so the result is deterministic and re-recordable when the tool changes.

```bash
brew install vhs          # macOS; see the VHS repo for Linux
pip install -e .          # so `polymarket-doctor` is on PATH
vhs docs/demo.tape        # writes docs/demo.gif, which the README embeds
```

`docs/demo.tape` runs the real onboarding against production with the public
addresses from py-clob-client-v2#70 — no credentials, no secrets. Edit the tape
to change size, theme, or which command runs.

To also show `verify-order` catching a bad order, add these lines to the tape
before the final `Sleep`:

```
Type "cat bad-order.json | polymarket-doctor verify-order --token <token>"
Enter
Sleep 4s
```

## Option B — asciinema (a shareable, replayable link)

Good when you want a link people can scrub through rather than a GIF.

```bash
brew install asciinema
asciinema rec demo.cast          # runs a shell; type your commands, then exit
asciinema upload demo.cast       # prints a shareable URL
```

Convert the cast to a GIF if you'd rather embed it:

```bash
brew install agg
agg demo.cast docs/demo.gif
```

## Option C — a narrated video (for a portfolio or an application)

For a 60–90 second walkthrough with your voice, record the screen and upload to
Loom or YouTube (unlisted), then link it.

- macOS: QuickTime Player → File → New Screen Recording, or press ⌘⇧5.
- A tight script that lands the story:
  1. "Placing an order on Polymarket's V2 exchange fails with an error that
     points nowhere near the cause. 49 open issues are this one bug."
  2. Run `onboard` against the #70 address. Point at the green
     `signature_type=2` line: "the tool reads the funder contract on-chain and
     tells you the exact signature type. Nothing in Polymarket's API exposes
     this."
  3. Run `verify-order` on a deliberately broken order. Point at the failing
     invariant: "and it checks the order your own code signs, before you send
     it."
  4. Close on the safety line: "it never places an order and never reads your
     private key."

Keep it under 90 seconds. Show the terminal, not slides.

## What to record

Use the address-only `onboard` run for anything public: it produces the full
staged output without touching credentials. Never record a session where
`POLYMARKET_PRIVATE_KEY` or the L2 secret is on screen or in shell history.
