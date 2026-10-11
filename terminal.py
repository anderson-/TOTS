import os
import sys
import tty
import math
import time
import random
import select
import shutil
import termios
import unicodedata
from dataclasses import dataclass

from theme import (BG_COLORS, DEFAULT_THEME, FG_COLORS, PRESETS, THEME_ITEMS,
                   color_name, parse_theme, theme_string)


def clean(text, width):
    result, used = [], 0
    for char in str(text):
        char = char if char.isprintable() else '?'
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if used+size > width:
            break
        result.append(char)
        used += size
    return ''.join(result)


def wrapped(text, width):
    offset = 0
    for line in text.split('\n'):
        if not line:
            yield '', offset
        while line:
            part = clean(line, width)
            if part:
                yield part, offset
            size = len(part) or 1
            line, offset = line[size:], offset+size
        offset += 1


def tiles(width, start, end, count):
    columns = min(count, max(1, width//24))
    if not columns or end <= start:
        return
    count = min(count, columns*(end-start))
    rows = math.ceil(count/columns)
    for row in range(rows):
        cells = min(columns, count-row*columns)
        top, bottom = start+row*(end-start)//rows, start+(row+1)*(end-start)//rows
        for column in range(cells):
            yield column*width//cells, top, (column+1)*width//cells-column*width//cells, bottom-top


def centered(text, width, center=True):
    text = clean(text, width)
    used = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in text)
    padding = width-used
    left = padding//2 if center else 0
    return ' '*left+text+' '*(padding-left)


def compact(value, rate=False):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            number = f'{value:.1f}' if unit != 'B' and value < 100 else f'{value:.0f}'
            return number, unit+('/s' if rate else '')
        value /= 1024


def displacement(t, direction, duration, size):
    def ease(p):
        return 4*p**3 if p < .5 else 1-(-2*p+2)**3/2
    dist, recoil, over = 1.25*size, .2*size, .1*size
    total = recoil+dist+recoil+over+over
    t1, t2 = duration*recoil/total, duration*(dist+recoil+over)/total
    if t < t1:
        return direction*(dist+recoil*ease(t/t1))
    if t < t1+t2:
        return direction*((dist+recoil+over)*(1-ease((t-t1)/t2))-over)
    return -direction*over*(1-ease((t-t1-t2)/(duration-t1-t2)))


@dataclass
class Slot:
    current: str = ''
    new: str = ''
    waiting: object = None
    elapsed: object = None
    direction: int = 1
    inverted: bool = False
    next_inverted: bool = False
    rule: str = ''
    new_rule: str = ''
    bridge: str = ''
    bridge_inverted: bool = False


class Terminal:
    duration = .6
    bridge_chance = .2

    def __init__(self, glyphs, ascii=None, debug=False, theme=None):
        self.glyphs, self.unicode, self.debug = glyphs, ascii is None, debug
        self.pixel = ascii or '#'
        self.theme = list(theme or parse_theme(DEFAULT_THEME))
        self.initial_theme = tuple(self.theme)
        self.preset = next((i for i, (_, codes) in enumerate(PRESETS)
                            if parse_theme(codes) == self.initial_theme), -1)
        self.theme_item, self.theme_until = 0, 0
        self.pool = [c for c in glyphs if 0x1f300 <= ord(c) <= 0x1faff]
        self.pool = self.pool or list(glyphs)
        self.slots = [Slot() for _ in range(3)]
        self.previous, self.size = [], None
        self.pending, self.escape_since = b'', None
        self.last_roll = 0
        self.last_source = ''
        self.pause_requested, self.paused, self.deferred = False, False, None
        self.body_key, self.body = None, []
        self.draw_key, self.info, self.next_info, self.next_draw = None, None, 0, 0

    def theme_key(self, key, now):
        if key in ('p', 'P'):
            self.preset = (self.preset+(1 if key == 'p' else -1)) % len(PRESETS)
            self.theme[:] = parse_theme(PRESETS[self.preset][1])
        elif key == 'T':
            self.theme_item = (self.theme_item+1) % len(self.theme)
        elif key == 't':
            palette = BG_COLORS if self.theme_item == 0 else FG_COLORS
            self.theme[self.theme_item] = palette[(palette.index(self.theme[self.theme_item])+1) % len(palette)]
        self.theme_until = now+3

    def style(self, foreground=None, background=None):
        fg = self.theme[1] if foreground is None else foreground
        bg = self.theme[0] if background is None else background
        return f'\x1b[0;{fg};{bg}m'

    def insert(self, emojis, now, immediate=False, rules=None, source="manual",
               shuffle=True):
        emojis, rules = list(emojis), rules or {}
        if self.pause_requested:
            self.deferred = emojis, immediate, rules, source, shuffle
            return
        self.deferred = None
        occupied = {c for s in self.slots for c in
                    (s.current, s.bridge, s.new if s.elapsed is not None else '',
                     s.waiting[0] if s.waiting else '')}
        fresh = list(dict.fromkeys(c for c in emojis if c in self.glyphs and c not in occupied))
        free = [s for s in self.slots if not s.waiting and s.elapsed is None]
        if shuffle:
            random.shuffle(free)
            random.shuffle(fresh)
        light = sum(s.next_inverted if s.elapsed is not None or s.waiting else s.inverted
                    for s in self.slots)
        for slot, char in zip(free, fresh):
            rule = rules.get(char, source)
            slot.direction = random.choice((-1, 1))
            slot.next_inverted = light < 2 and random.random() < .5
            light += slot.next_inverted
            pool = [c for c in self.pool if c != char and c != slot.current]
            slot.bridge = random.choice(pool) if pool and random.random() < self.bridge_chance else ''
            slot.bridge_inverted = random.random() < .5
            slot.waiting = (char, now+(0 if immediate else random.uniform(0, .7)), rule)
            self.last_roll = now

    def toggle_pause(self, now):
        self.pause_requested = not self.pause_requested
        self.paused = self.pause_requested and all(s.elapsed is None and not s.waiting for s in self.slots)
        self.last_roll = now

    def advance(self, dt, now):
        if self.paused:
            return
        for slot in self.slots:
            if slot.waiting and now >= slot.waiting[1]:
                slot.new, slot.new_rule = slot.waiting[0], slot.waiting[2]
                slot.elapsed = max(0, now-slot.waiting[1])
                slot.waiting = None
            elif slot.elapsed is not None:
                slot.elapsed += min(dt, .1)
            span = 2*self.duration if slot.bridge else self.duration
            if slot.elapsed is not None and slot.elapsed >= span:
                slot.current, slot.inverted, slot.rule = slot.new, slot.next_inverted, slot.new_rule
                slot.bridge, slot.elapsed = '', None
        if all(s.elapsed is None and not s.waiting for s in self.slots):
            if self.pause_requested:
                self.paused = True
            elif self.deferred is not None:
                emojis, immediate, rules, source, shuffle = self.deferred
                self.insert(emojis, now, immediate, rules, source, shuffle)

    def deadline(self, now):
        deadlines = [s.waiting[1] for s in self.slots if s.waiting] if not self.paused else []
        if not self.paused and any(s.elapsed is not None for s in self.slots):
            deadlines.append(now+1/30)
        if self.pending:
            deadlines.append(now+.03)
        if self.next_draw > now:
            deadlines.append(self.next_draw)
        if self.theme_until > now:
            deadlines.append(self.theme_until)
        return min(deadlines, default=now+1)

    def slot_key(self):
        return tuple((s.current, s.inverted, s.new, s.next_inverted, s.bridge, s.elapsed, s.direction)
                     if s.elapsed is not None else (s.current, s.inverted) for s in self.slots)

    def layout(self, columns, lines):
        maximum, height_limit = min(columns-1, int(columns*3/5)), max(1, lines-4)
        modes = ((1, 2, maximum),) if self.unicode else \
                ((2, 1, maximum), (2, 1, columns-1), (1, 1, maximum))
        candidates = []
        for preference, (repeat, vertical, ceiling) in enumerate(modes):
            for base in range(22, 15, -2):
                for scale in range(1, ceiling//(3*base*repeat)+1):
                    width, height = 3*base*scale*repeat, base*scale//vertical
                    if columns/3 <= width <= ceiling and height <= height_limit:
                        score = (preference, 22-base, abs(width-columns/2))
                        candidates.append((score, base, width, height))
        if candidates:
            _, base, width, height = min(candidates)
            return base, width, height
        tile = max(1, min(int(columns/2)//3, 2*height_limit, (columns-1)//3))
        return 22, 3*tile, max(1, round(tile/2))

    def pixels(self, base):
        frame = [[0]*(3*base) for _ in range(base)]
        for index, slot in enumerate(self.slots):
            if slot.elapsed is None:
                layers = [(slot.current, slot.inverted, 0)]
            else:
                incoming = [(slot.new, slot.next_inverted)]
                if slot.bridge:
                    incoming.append((slot.bridge, slot.bridge_inverted))
                step = slot.direction*1.25*base
                dy = displacement(slot.elapsed, slot.direction,
                                  self.duration*len(incoming), base*len(incoming))
                layers = [(char, inverted, dy-rank*step)
                          for rank, (char, inverted) in enumerate(incoming)]
                layers.append((slot.current, slot.inverted, dy-len(incoming)*step))
            for char, inverted, offset in layers:
                glyph_width, rows = self.glyphs.get(char, (16, (0,)*16))
                gx, gy = (base-glyph_width)//2, (base-16)//2
                offset = math.ceil(offset)
                for y in range(max(0, offset), min(base, offset+base)):
                    row = y-offset-gy
                    for x in range(base):
                        col = x-gx
                        pixel = 0 <= row < 16 and 0 <= col < glyph_width and bool(rows[row] & (1 << col))
                        frame[y][index*base+x] = int(pixel != inverted)
        return frame

    def frame(self, store, cpu, rate, columns, lines, now=None, info=None):
        columns, lines = max(1, columns), max(1, lines)
        width = max(1, columns-1)
        rows, colors = ['']*lines, [self.theme[1]]*lines
        groups_count = 1 if width >= 79 else 2 if width >= 48 else 3
        info_top = lines-1-groups_count
        if width < 12 or info_top < 4:
            rows[0] = 'Terminal too small'
            return rows, colors
        base, fw, fh = self.layout(columns, info_top+1)
        left, top = (width-fw)//2, 1+(info_top-1-fh)//2
        body_key = columns, lines, self.slot_key()
        if self.body_key != body_key:
            body = ['']*lines
            pixels = self.pixels(base)
            vertical = 2 if self.unicode else 1
            for y in range(fh):
                row = []
                for x in range(fw):
                    sx = min(3*base-1, int((x+.5)*3*base/fw))
                    sy = min(base-1, int((y*vertical+.5)*base/(fh*vertical)))
                    first = pixels[sy][sx]
                    if self.unicode:
                        sy2 = min(base-1, int((y*2+1.5)*base/(fh*2)))
                        row.append(' ▄▀█'[first*2+pixels[sy2][sx]])
                    else:
                        row.append(self.pixel if first else ' ')
                body[top+y] = ' '*left+''.join(row)
            self.body_key, self.body = body_key, body
        rows = self.body[:]
        sessions, cpu, rate, total, queued = info or (len(store.sessions), cpu, rate, store.total, store.queued)
        number, unit = compact(rate, True)
        total, total_unit = compact(total)
        fields = [f'{sessions:3d} streams', f'{cpu:3.0f}% cpu',
                  f'{number:>6}{unit:<4} rate', f'{total:>6}{total_unit:<2} total',
                  f'{queued:3d} queue']
        groups = ([fields] if width >= 79 else [fields[:3], fields[3:]] if width >= 48
                  else [fields[:2], fields[2:4], fields[4:]])
        for offset, group in enumerate(groups):
            y = lines-1-len(groups)+offset
            row = [' ']*width
            for i, field in enumerate(group):
                start, end = i*width//len(group), (i+1)*width//len(group)
                field = field[-max(1, end-start-1):]
                x = start+(end-start-len(field))//2
                row[x:x+len(field)] = field
            rows[y], colors[y] = ''.join(row), self.theme[2]
        info_top = lines-1-len(groups)
        now = time.monotonic() if now is None else now
        if self.debug:
            debug = list(store.debug(now)) or [('No active streams', '', 36, [])]
            bands = [(a, b) for a, b in ((1, top-1), (top+fh+1, info_top)) if b > a]
            panels, offset = [], 0
            for index, (start, end) in enumerate(bands):
                count = len(debug)//len(bands)+(index < len(debug)%len(bands))
                cells = list(tiles(width, start, end, count))
                panels.extend(zip(cells, debug[offset:offset+len(cells)]))
                offset += len(cells)
            for y in (y for start, end in bands for y in range(start, end)):
                rows[y], colors[y] = '', []
            for (x, y, cell, height), (label, text, role, marks) in panels:
                content = [(clean(label, cell-2), -1)]
                if height > 1:
                    content += list(wrapped(text, cell-2))[-(height-1):]
                for row in range(height):
                    line, position = content[row] if row < len(content) else ('', -1)
                    color = self.theme[3 if row == 0 or role == 36 else 4 if role == 33 else 5]
                    edges = sorted({0, len(line), *(max(0, min(len(line), edge-position))
                                    for span in marks for edge in span)}) if position >= 0 else [0, len(line)]
                    chunks = [(' ', color, False)]
                    chunks += [(line[a:b], color, position >= 0 and any(left <= position+a < right for left, right in marks))
                               for a, b in zip(edges, edges[1:])]
                    chunks.append((centered(line, cell-2, False)[len(line):]+' ', color, False))
                    rows[y+row] += ''.join(part for part, _, _ in chunks)
                    colors[y+row].extend(chunks)
            if top > 0:
                rows[top-1] = ' '*left+centered(self.last_source or '-', fw)
                colors[top-1] = self.theme[3]
            if top+fh < info_top:
                cell = fw//3
                rows[top+fh] = ' '*left+''.join(centered(s.rule, cell) for s in self.slots)
                colors[top+fh] = self.theme[3]
        if now < self.theme_until:
            name = next((name+' ' for name, codes in PRESETS
                         if parse_theme(codes) == tuple(self.theme)), '')
            item = color_name(self.theme[self.theme_item], self.theme_item == 0)
            header, color = (f'theme {name}{THEME_ITEMS[self.theme_item]}={item} '
                             f'{theme_string(self.theme)}'), (97, 40)
        elif self.debug or self.pause_requested:
            header = 'PAUSED' if self.paused else 'PAUSING' if self.pause_requested else 'DEBUG'
            color = self.theme[3]
        else:
            header, color = '', None
        if self.debug:
            size = clean(f'{columns}x{lines}', max(0, width-len(header)-1))
            start = max(0, min((width-len(size))//2, width-len(header)-len(size)-1))
            row = list(rows[0].ljust(width))
            row[start:start+len(size)] = size
            rows[0], colors[0] = ''.join(row), self.theme[3]
        if header:
            header = clean(header, width)
            start = max(0, width-len(header))
            row = list(rows[0].ljust(width))
            row[start:start+len(header)] = header
            rows[0], colors[0] = ''.join(row), color
        return rows, colors

    def draw(self, store, cpu, rate, now=None):
        now = time.monotonic() if now is None else now
        columns, lines = shutil.get_terminal_size()
        if now < self.next_draw:
            return
        if now >= self.next_info:
            self.info = len(store.sessions), cpu, rate, store.total, store.queued
            self.next_info = now+1
        count, shown_cpu, shown_rate, total, queue = self.info
        info_key = count, f'{shown_cpu:.0f}', compact(shown_rate, True), compact(total), queue
        toast = (self.theme_item, self.theme_until) if now < self.theme_until else None
        draw_key = (columns, lines, tuple(self.theme), self.slot_key(), info_key, self.debug,
                    self.pause_requested, self.paused,
                    (store.revision, self.last_source, tuple(s.rule for s in self.slots),
                     tuple((name, int(elapsed)) for name, elapsed in store.tool_status(now))) if self.debug else None,
                    toast)
        if self.draw_key == draw_key:
            return
        self.draw_key, self.next_draw = draw_key, now+1/30
        appearance = (columns, lines, tuple(self.theme))
        if self.size != appearance:
            sys.stdout.write(self.style()+'\x1b[2J')
            self.previous, self.size = [], appearance
        rows, colors = self.frame(store, cpu, rate, columns, lines, now, self.info)
        output, current = [], []
        for y, (row, color) in enumerate(zip(rows, colors)):
            row = clean(row, max(1, columns-1))
            entry = row, color
            if y >= len(self.previous) or self.previous[y] != entry:
                if isinstance(color, list):
                    styled = ''.join(self.style(fg)+('\x1b[7m' if reverse else '')+text
                                     for text, fg, reverse in color)
                elif isinstance(color, tuple):
                    caption = row.lstrip(' ')
                    padding = row[:len(row)-len(caption)]
                    styled = self.style()+padding+self.style(*color)+caption
                else:
                    styled = self.style(color)+row
                output.append(f'\x1b[{y+1};1H{styled}{self.style()}\x1b[K')
            current.append(entry)
        if output:
            sys.stdout.write(''.join(output))
            sys.stdout.flush()
        self.previous = current

    def keys(self, now):
        if select.select([sys.stdin], [], [], 0)[0]:
            data = os.read(sys.stdin.fileno(), 128)
            if not data:
                yield 'q'
                return
            self.pending += data
        while self.pending:
            if self.pending.startswith(b'\x1b'):
                self.escape_since = now if self.escape_since is None else self.escape_since
                if len(self.pending) == 1:
                    if now-self.escape_since < .05:
                        return
                    self.pending, self.escape_since = b'', None
                    yield '\x1b'
                    continue
                if self.pending[1:2] in (b'[', b'O'):
                    end = next((i for i in range(2, len(self.pending))
                                if 0x40 <= self.pending[i] <= 0x7e), None)
                    if end is None:
                        if now-self.escape_since < .1:
                            return
                        self.pending = b''
                    else:
                        self.pending = self.pending[end+1:]
                else:
                    self.pending = self.pending[2:]
                self.escape_since = None
                continue
            key = chr(self.pending[0])
            self.pending, self.escape_since = self.pending[1:], None
            yield key

    def __enter__(self):
        self.original = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write('\x1b[?1049h'+self.style()+'\x1b[2J\x1b[?25l')
        sys.stdout.flush()
        return self

    def __exit__(self, *_):
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.original)
        finally:
            sys.stdout.write('\x1b[0;39;49m\x1b[?25h\x1b[?1049l')
            sys.stdout.flush()
