#!/usr/bin/env python3
"""Concurrent sessions alternating reasoning and output, with paragraphs and tool events."""

import os
import random
import signal
import socket
import argparse
import threading

PARAGRAPHS = [
    'Let me check the terminal size before drawing anything. '
    'The columns and lines look sane. '
    'Now I have the layout cached, so the slots can roll in. All good.\n\n',

    'The stream arrives in small chunks. '
    'I need to buffer them until a paragraph shows up. '
    'Once the boundary lands, the matcher runs and the slots update. '
    "That's fine for now.\n\n",

    'Reading the font failed on the first glyph. '
    'Let me verify the BDF header. '
    "There's a mismatch in the ascent value. "
    'Actually the file is gzipped, which explains it.\n\n',

    'Let me read the session store again. '
    'The queued batch is always the newest one. '
    'But wait — the older text is dropped on purpose. '
    'I should handle the empty case too.\n\n',

    'The socket is bound and the poll loop wakes on every datagram. '
    'Nothing surprising in the counters yet, so the display keeps the previous slots. '
    'Two more paragraphs are still in flight.\n\n',

    'Let me run the test suite and see what breaks. '
    'The first failure is a rounding error in the progress bar. '
    'Let me reconsider the arithmetic — integer division would be cleaner. Done.\n\n',

    'Let me grep for the old symbol across the repo. '
    'It still shows up in two files. '
    "But there's nothing importing it, so a rename is safe. "
    'Let me also update the docs.\n\n',

    'Let me search the logs for the timeout. Nothing there. '
    'Let me look at the retry loop instead. '
    'Wait but the backoff is capped at ninety seconds.\n\n',

    'Let me start with the parser. '
    'The tokens are split on punctuation and dashes. '
    "I'll use the same boundary regex as the matcher. "
    'Let me write the new rule and test it.\n\n',

    'Let me add a fallback for missing glyphs. '
    'If an emoji has no bitmap, the slot is skipped silently. '
    'Also check the cache before redrawing. '
    'That keeps the idle loop cheap.\n\n',

    'Hmm but the counts never settle. '
    'I can reproduce it with two sessions and a slow writer. '
    'The buffer keeps the last paragraph, which is by design.\n\n',

    'Let me think about the flush timing. '
    'Pending text is released after a short quiet period. '
    'Actually let me wait for two full sentences instead. '
    'Let me reconsider.\n\n',

    'Let me look at the theme table to see the presets. '
    'The amber and green entries are neighbours. '
    'Let me use the classic one for the demo. '
    'Now I have something readable.\n\n',

    'Let me add the cache to the renderer. '
    'The file is rewritten in place, then the demo reruns. '
    'The stable pixels are reused, so the redraw stays cheap.\n\n',

    'Let me search the test fixtures for the old constant. '
    'It shows up in the docs as well. '
    'Let me write the updated list and rerun. '
    'But the fixtures still need a pass.\n\n',
]

TOOLS = ('read_file', 'search_files', 'write_file', 'terminal', 'patch')

LOREM = (
    'Lorem ipsum dolor sit amet consectetur adipiscing elit. '
    'Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. '
    'Ut enim ad minim veniam quis nostrud exercitation ullamco laboris. '
    'Duis aute irure dolor in reprehenderit in voluptate velit esse cillum. '
    'Excepteur sint occaecat cupidatat non proident sunt in culpa qui officia. '
)


def tremor(fast=(.02, .06), slow=(.05, .2), stall=.05):
    slow_tokens = 0
    while True:
        if not slow_tokens and random.random() < stall:
            slow_tokens = random.randint(3, 6)
        if slow_tokens:
            slow_tokens -= 1
            yield random.uniform(*slow)
        else:
            yield random.uniform(*fast)


def emit(sock, path, stop, *fields):
    try:
        sock.sendto('\x1f'.join(fields).encode('utf-8'), path)
        return True
    except OSError:
        if stop is not None:
            stop.wait(.5)
        return False


def session(path, sid, platform, kind, stop, paragraphs=PARAGRAPHS):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        pace, sequence = tremor(fast=(.03, .12), slow=(.1, .3)), 0
        while not stop.is_set():
            text = ''.join(random.choices(paragraphs, k=random.randint(1, 3)))
            while text and not stop.is_set():
                size = random.randint(3, 8)
                chunk, text = text[:size], text[size:]
                sequence += 1
                if not emit(sock, path, stop, str(os.getpid()), platform, sid, kind, sid, str(sequence), chunk):
                    return
                stop.wait(next(pace))
            if not stop.is_set():
                stop.wait(random.uniform(.3, 1))
                if not stop.is_set():
                    kind = 'T' if kind == 'R' else 'R'
        emit(sock, path, None, str(os.getpid()), platform, sid, kind, sid, str(sequence+1), '\n\n')
        return f'{sid} {kind}'


def lorem(path, sid, platform, stop):
    return session(path, sid, platform, 'T', stop, [LOREM+'\n\n'])


def tools(path, sid, stop):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        while not stop.is_set():
            writer = 'tool_call_id:%d' % random.randrange(1 << 20)
            name = random.choice(TOOLS)
            emit(sock, path, stop, '0', '', sid, 'S', writer, '1', name)
            if stop.wait(random.uniform(1, 4)):
                emit(sock, path, None, '0', '', sid, 'E', writer, '2', name)
                return f'{sid} tool'
            emit(sock, path, stop, '0', '', sid, 'E', writer, '2', name)
            stop.wait(random.uniform(4, 10))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/tmp/hermes-stream.sock')
    parser.add_argument('--seed', type=int, help='optional rand seed')
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    stop = threading.Event()

    def on_signal(*_):
        stop.set()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    streams = [
        (session, ('test-0', 'telegram', 'R')),
        (session, ('test-1', 'agent', 'T')),
        (lorem, ('test-2', 'cli')),
        (tools, ('test-1',)),
    ]
    closed = {}

    def run(index, target, rest):
        closed[index] = target(args.socket, *rest, stop)

    threads = [threading.Thread(target=run, args=(index, target, rest), daemon=True)
               for index, (target, rest) in enumerate(streams)]
    for thread in threads:
        thread.start()
    while not stop.wait(1):
        pass
    for thread in threads:
        thread.join()
    flushed = ', '.join(closed[index] for index in sorted(closed) if closed[index])
    print('closed on shutdown: ' + flushed if flushed else 'shutdown: nothing was open')


if __name__ == '__main__':
    main()
