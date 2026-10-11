Output assets/vhs/debug.gif
Output assets/vhs/debug.mp4

Source assets/vhs/term.tape

Set TypingSpeed 0ms

Hide
Type "sh -c '{ sleep 3; ./tools/feed.py --seed 7 --sessions 9 >/dev/null & m=$!; sleep 15; kill $m; } & ./main.py --seed 7'"
Enter
Wait+Screen /queue/
Show
Sleep 2s

Type d
Sleep 30s

Type q
