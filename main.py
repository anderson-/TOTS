#!/usr/bin/env python3
"""Terminal screensaver: system Python, CSV rules and official bitmap glyphs."""
import sys
import time
import shlex
import random
import shutil
import select
import signal
import argparse
import importlib
from pathlib import Path
from collections import deque

from stream import Store
from render import render
from terminal import Terminal
from theme import parse_theme, theme_string
from matcher import IDLE, TOOL, THINK, STARTUP, Matcher, emoji_chars, load_font

ASSETS = Path(__file__).resolve().parent / 'assets'


def ascii_pixel(value):
    if len(value) != 1 or not 33 <= ord(value) <= 126:
        raise argparse.ArgumentTypeError('--ascii requires one visible ASCII character')
    return value


def theme_codes(value):
    try:
        return parse_theme(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def render_wrapped(glyphs, chars, columns, unicode=True, pixel='#'):
    chars_per_row = max(1, columns // 16)
    for start in range(0, len(chars), chars_per_row):
        yield from render(glyphs, chars[start:start+chars_per_row], unicode, pixel)


def theme_command(theme, argv=None):
    arguments = list(argv if argv is not None else
                     getattr(sys, 'orig_argv', [sys.executable, *sys.argv]))
    command, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--theme':
            index += 2
            continue
        if not argument.startswith('--theme='):
            command.append(argument)
        index += 1
    return shlex.join([*command, '--theme', theme_string(theme)])


def cpu_stat():
    try:
        with open('/proc/stat') as source:
            fields = [int(n) for n in source.readline().split()[1:9]]
        return sum(fields), sum(fields)-fields[3]-fields[4]
    except (OSError, ValueError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ascii', nargs='?', const='#', type=ascii_pixel,
                        help='use ASCII pixels, optionally specifying a character (default: #)')
    parser.add_argument('--theme', type=theme_codes,
                        help='six colors bg fg info debug thought output, no separators: '
                             'bk rd gn yl bl mg cy wh fg bg, + for bright (e.g. bgfgwhcygnyl)')
    parser.add_argument('--debug', action='store_true', help='show stream buffers')
    parser.add_argument('--csv', type=Path, default=ASSETS/'thoughts.csv', help='thought,emoji CSV')
    parser.add_argument('--words', type=int, default=3, help='maximum words matched at either boundary')
    parser.add_argument('--converter', help='optional module:function(text) returning emoji characters (any number)')
    parser.add_argument('--socket', default='/tmp/hermes-stream.sock', help='Hermes Unix datagram socket')
    parser.add_argument('--demo', action='store_true', help='start in demo mode without opening a socket (D toggles demo)')
    parser.add_argument('--render-csv', action='store_true', help='print each CSV entry with its emojis, then exit')
    parser.add_argument('--render', metavar='STRING',
                        help='render the given emoji characters, then exit (1 if any is missing from the font)')
    parser.add_argument('--seed', type=int, help='optional rand seed')
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    if args.words < 1:
        parser.error('--words must be positive')
    if args.ascii is None:
        try:
            '▀▄█'.encode(sys.stdout.encoding or 'utf-8')
        except UnicodeEncodeError:
            parser.error('output encoding does not support blocks; use --ascii')
    try:
        matcher = Matcher(args.csv, args.words)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.render_csv:
        try:
            fixed_items = (('Startup', STARTUP), ('Idle', IDLE),
                           ('Tool', TOOL), ('Think', THINK))
            wanted = matcher.emojis | set(''.join(chars for _, chars in fixed_items))
            glyphs = load_font(ASSETS/'UnifontExMono-16.bdf.gz', wanted)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        for label, chars in fixed_items:
            print(label)
            available = [c for c in chars if c in glyphs]
            for line in render_wrapped(glyphs, available, columns,
                                       args.ascii is None, args.ascii or '#'):
                print(line)
            print()
        for thought, chars in matcher.rows:
            print(thought)
            available = [c for c in chars if c in glyphs]
            for line in render_wrapped(glyphs, available, columns,
                                       args.ascii is None, args.ascii or '#'):
                print(line)
            print()
        return
    if args.render is not None:
        chars = [c for c in emoji_chars(args.render) if not c.isspace()]
        try:
            glyphs = load_font(ASSETS/'UnifontExMono-16.bdf.gz', set(chars))
        except ValueError:
            glyphs = {}
        except OSError as error:
            parser.error(str(error))
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        available = [c for c in chars if c in glyphs]
        for line in render_wrapped(glyphs, available, columns,
                                   args.ascii is None, args.ascii or '#'):
            print(line)
        sys.exit(1 if len(available) != len(chars) else 0)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('an interactive terminal is required')
    try:
        requested = matcher.emojis | set(IDLE) | set(TOOL) | set(THINK)
        wanted = requested | set(STARTUP) | {chr(cp) for cp in range(0x1f300, 0x1fb00)}
        glyphs = load_font(ASSETS/'UnifontExMono-16.bdf.gz', None if args.converter else wanted)
        missing = sorted(matcher.emojis-glyphs.keys(), key=ord)
        matcher.resolve(glyphs)
        convert = matcher
        if args.converter:
            module, separator, name = args.converter.partition(':')
            if not separator:
                raise ValueError('--converter must be module:function')
            convert = getattr(importlib.import_module(module), name)
            if not callable(convert):
                raise ValueError('converter must be callable')
        if missing:
            print('Warning: missing glyphs will be skipped:')
            for char in missing:
                print(f'  U+{ord(char):04X} {char!r}')
            input('[Enter] to continue')
    except (OSError, ValueError, ImportError, AttributeError) as error:
        parser.error(str(error))
    idle_pool = [c for c in IDLE if c in glyphs]
    tool_pool = [c for c in TOOL if c in glyphs]
    think_pool = [c for c in THINK if c in glyphs]
    terminal = Terminal(glyphs, args.ascii, args.debug, args.theme)
    store = Store()
    previous = next_demo = next_tool = last_activity = time.monotonic()
    terminal.insert([c for c in STARTUP if c in glyphs], previous, shuffle=False)
    sleeping, demo = False, args.demo
    was_thinking, think_pending = False, False
    next_cpu, previous_cpu, cpu, rate = previous, None, 0, 0
    history = deque([(previous, 0)])
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    sys.stdout.write('\x1b[2J\x1b[H')
    try:
        if not demo:
            store.open(args.socket)
        with terminal:
            running = True
            while running:
                now = time.monotonic()
                for key in terminal.keys(now):
                    if key in ('q', '\x1b'):
                        running = False
                    elif key == ' ':
                        terminal.toggle_pause(now)
                        if not terminal.pause_requested:
                            last_activity, sleeping, next_demo = now, False, now+2
                    elif key == 'd':
                        terminal.debug = not terminal.debug
                    elif key == 'D':
                        if demo and store.socket is None:
                            store.open(args.socket)
                        demo = not demo
                        last_activity, sleeping, next_demo = now, False, now
                        think_pending = False
                        terminal.deferred = None
                    elif key in ('t', 'T', 'p', 'P'):
                        terminal.theme_key(key, now)
                    elif key in '0123456789':
                        terminal.insert([random.choice(terminal.pool)], now, immediate=True)
                        last_activity, sleeping = now, False
                if not running:
                    break
                text = store.poll(now)
                think_pending = not demo and (think_pending or (store.thinking and not was_thinking))
                was_thinking = store.thinking
                is_tool = bool(text and store.source.startswith('tool / '))
                if text and not demo:
                    emojis = list(dict.fromkeys(
                        c for c in emoji_chars(''.join(convert(text))) if c in glyphs))
                    if is_tool and not emojis:
                        emojis = random.sample(tool_pool, min(3, len(tool_pool)))
                    if emojis:
                        terminal.last_source = store.source
                        think_pending = False
                    terminal.insert(emojis, now, rules=matcher.rules if convert is matcher else None,
                                    source='tool' if is_tool else 'converter')
                    last_activity, sleeping = now, False
                if demo and not terminal.pause_requested and now >= next_demo:
                    terminal.insert(random.sample(terminal.pool, min(3, len(terminal.pool))), now,
                                    source="demo")
                    next_demo = now+2
                if not demo and store.busy and not terminal.pause_requested:
                    sleeping = False
                    last_activity = now
                    if now >= next_tool and not (text and is_tool):
                        names = [name for name, _ in store.tool_status(now) if name]
                        emojis = list(dict.fromkeys(c for c in emoji_chars(''.join(convert(
                            '\n\n'.join('*' + name for name in names)))) if c in glyphs))
                        if not emojis:
                            emojis = random.sample(tool_pool, min(3, len(tool_pool)))
                        terminal.last_source = 'tool / ' + ', '.join(names)
                        terminal.insert(emojis, now,
                                        rules=matcher.rules if convert is matcher else None, source='tool')
                        next_tool = now+2
                    elif text and is_tool:
                        next_tool = now+2
                if think_pending and think_pool and not terminal.pause_requested \
                        and all(s.elapsed is None and not s.waiting for s in terminal.slots):
                    terminal.insert(random.sample(think_pool, min(3, len(think_pool))),
                                    now, source="think")
                    last_activity, sleeping, think_pending = now, False, False
                activity = max(last_activity, terminal.last_roll)
                if not demo and not terminal.pause_requested and not sleeping and not store.busy and not store.thinking and now-activity > 10:
                    terminal.insert(random.sample(idle_pool, min(3, len(idle_pool))), now, source="idle")
                    sleeping = True
                terminal.advance(now-previous, now)
                if now >= next_cpu:
                    history.append((now, store.total))
                    while len(history) > 1 and history[1][0] <= now-3:
                        history.popleft()
                    span = now-history[0][0]
                    rate = (store.total-history[0][1])/span if span > 0 else 0
                    stat = cpu_stat()
                    if stat and previous_cpu and stat[0] > previous_cpu[0]:
                        cpu = min(100, max(0, 100*(stat[1]-previous_cpu[1])/(stat[0]-previous_cpu[0])))
                    previous_cpu, next_cpu = stat, now+1
                terminal.draw(store, cpu, rate, now)
                previous = now
                deadline = min(now+.25, next_cpu, store.deadline(now), terminal.deadline(now))
                readers = [sys.stdin] + ([store.socket] if store.socket else [])
                select.select(readers, [], [], max(0, deadline-time.monotonic()))

    finally:
        store.close()
        sys.stdout.write('\x1b[2J\x1b[H')
        if tuple(terminal.theme) != terminal.initial_theme:
            print(theme_command(terminal.theme))


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
    except (OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
