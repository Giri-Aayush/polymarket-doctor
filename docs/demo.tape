# VHS tape for the README demo GIF.
# Install VHS once:  brew install vhs   (or: go install github.com/charmbracelet/vhs@latest)
# Render:            vhs docs/demo.tape  ->  writes docs/demo.gif
#
# This runs the real tool against production with the public addresses from
# py-clob-client-v2#70. No credentials, no secrets: it shows the environment and
# identity stages classifying a Gnosis Safe funder, which is the whole point.

Output docs/demo.gif

Set Shell bash
Set FontSize 18
Set Width 1180
Set Height 780
Set Padding 24
Set Theme "Dracula"
Set TypingSpeed 45ms

# Prompt, then the command typed out.
Type "polymarket-doctor onboard \"
Enter
Type "  --address 0x9F49475F9496c77fa95f76c7C5Bc57467B336792 \"
Enter
Type "  --funder  0x3EC7EEaa66d849a5F83bE99a3ef47c63f672d649"
Sleep 700ms
Enter

# Let the live run complete (it hits production; ~4s plus the websocket frame).
Sleep 7s

# Hold the final frame so a reader can take it in.
Sleep 3s
