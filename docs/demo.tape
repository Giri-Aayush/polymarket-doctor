# VHS tape for the README demo GIF. Renders a three-beat terminal demo.
#
#   Install once:  brew install vhs
#   Render:        vhs docs/demo.tape   ->  writes docs/demo.gif
#
# All public data: the addresses are from py-clob-client-v2#70, and the sample
# order is signed with a throwaway test key. No credentials, nothing to redact.

Output docs/demo.gif

Set Shell bash
Set FontSize 18
Set Width 1180
Set Height 940
Set Padding 24
Set Theme "Dracula"
Set TypingSpeed 40ms

# --- Beat 1: classify a Gnosis Safe funder (the headline) ---
Type "polymarket-doctor onboard \"
Enter
Type "  --address 0x9F49475F9496c77fa95f76c7C5Bc57467B336792 \"
Enter
Type "  --funder  0x3EC7EEaa66d849a5F83bE99a3ef47c63f672d649"
Sleep 500ms
Enter
Sleep 8s

Type "clear"
Enter
Sleep 500ms

# --- Beat 2: verify an order your own code produced ---
Type "polymarket-doctor verify-order --file docs/samples/broken-order.json"
Sleep 500ms
Enter
Sleep 4s

# Hold the final frame.
Sleep 3s
