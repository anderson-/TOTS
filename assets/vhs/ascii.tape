Output assets/vhs/ascii.gif
Output assets/vhs/ascii.mp4

Source assets/vhs/term.tape

Set TypingSpeed 0ms

Hide
Type "sh -c '{ sleep 3; ./tools/feed.py --seed 7 >/dev/null & m=$!; sleep 20; kill $m; } & ./main.py --seed 7 --ascii'"
Enter
Wait+Screen /queue/
Show
Sleep 15s

Type d
Sleep 5s

Type q
