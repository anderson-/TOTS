import os
import re
import stat
import socket
from collections import deque
from dataclasses import dataclass, field

PRIORITY = {'telegram': 5, 'cli': 3, 'agent': 2, 'cron': 1}
SENTENCE = re.compile(r'[.!?。！？]+(?=\s|$)')


@dataclass
class Session:
    platform: str
    last: float
    sequence: int = 0
    kind: str = 'T'
    buffer: str = ''
    display: str = ''
    pending: str = ''
    queued: float = 0
    count: int = 0
    offset: int = 0
    pending_start: int = 0
    marks: list = field(default_factory=list)

    def switch(self, kind):
        self.kind, self.buffer, self.display, self.pending = kind, '', '', ''
        self.count, self.offset, self.marks = 0, 0, []

    def feed(self, text, now):
        self.count += len(text.encode('utf-8'))
        self.buffer = (self.buffer+text)[-65536:]
        display = self.display+text.replace('\r', '')
        self.offset += max(0, len(display)-65536)
        self.display = display[-65536:]
        self.marks = [(a, b) for a, b in self.marks if b > self.offset]
        boundaries = list(re.finditer(r'\n\s*\n', self.buffer))
        if boundaries:
            end = boundaries[-1].end()
            self.queue(self.buffer[:end], now)
            self.buffer = self.buffer[end:]
        completed = list(SENTENCE.finditer(self.buffer))
        if len(completed) >= 2:
            end = completed[-1].end()
            self.queue(self.buffer[:end], now)
            self.buffer = self.buffer[end:]

    def queue(self, text, now):
        text = text.replace('\r', '').strip()
        if text:
            self.pending, self.queued = text, now
            self.pending_start = self.offset+self.display.rfind(text)

    def flush(self, now):
        if self.buffer.rstrip().endswith(('.', '!', '?', '。', '！', '？')):
            self.queue(self.buffer, now)
            self.buffer = ''


class Store:
    idle_timeout, session_timeout = 10, 90

    def __init__(self):
        self.sessions = {}
        self.total = 0
        self.socket = None
        self.path = None
        self.identity = None
        self.pidfile = None
        self.last_conversion = 0
        self.revision, self.source = 0, ''
        self.tools = {}
        self.tool_pending = deque(maxlen=64)
        self.selected, self.now = [], 0

    def open(self, path):
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise OSError(f'Path is not a socket: {path}')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
                try:
                    probe.connect(path)
                except ConnectionRefusedError:
                    os.unlink(path)
                else:
                    raise OSError(f'Socket is already in use: {path}')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind(path)
            sock.setblocking(False)
            self.path, self.socket = path, sock
            info = os.stat(path)
            self.identity = info.st_dev, info.st_ino
        except BaseException:
            sock.close()
            raise
        self.pidfile = os.path.splitext(path)[0] + '.pid'
        try:
            with open(self.pidfile, 'w') as handle:
                handle.write(f'{os.getpid()}\n')
        except OSError:
            self.pidfile = None

    def absorb(self, data, now):
        self.now = now
        parts = data.decode('utf-8', 'replace').split('\x1f', 6)
        if len(parts) == 7:
            pid, platform, sid, kind, writer, sequence, text = parts
        elif len(parts) == 5:
            pid, platform, sid, kind, text = parts
            writer = sequence = ''
        elif len(parts) == 2:
            platform, text = parts
            pid = sid = writer = sequence = ''
            kind = 'T'
        else:
            return
        if kind == 'S':
            candidates = [(key, session) for key, session in self.sessions.items() if key[0] == pid and key[2] == sid]
            key, session = max(candidates, key=lambda entry: entry[1].last, default=((pid, platform, sid, ''), None))
            if session is None:
                session = self.sessions[key] = Session(platform, now)
            if session.kind != 'S':
                session.switch('S')
            self.tools[pid, sid, writer] = (text, now, session)
            session.display = '\n'.join(name for name, _, owner in self.tools.values() if owner is session)
            session.last, session.marks = now, []
            if text:
                self.tool_pending.append((session, text))
            self.revision += 1
            return
        if kind == 'E':
            self.end_tool((pid, sid, writer), now)
            return
        if kind not in ('T', 'R'):
            return
        try:
            seq = int(sequence) if sequence else 0
        except ValueError:
            return
        key = (pid, platform, sid, writer)
        session = self.sessions.get(key)
        if session is None:
            placeholder = next((k for k in self.sessions if k[0] == pid and k[2] == sid and not k[3] and k[1] in ('', platform)), None)
            session = self.sessions.pop(placeholder) if placeholder is not None else Session(platform, now)
            session.platform = platform
            self.sessions[key] = session
        if session.kind != kind or (writer and seq and session.sequence and seq != session.sequence+1):
            session.switch(kind)
        self.tools = {key: entry for key, entry in self.tools.items() if entry[2] is not session}
        if writer and seq:
            session.sequence = seq
        session.feed(text, now)
        self.total += len(text.encode('utf-8'))
        session.last = now
        self.revision += 1

    def end_tool(self, key, now):
        entry = self.tools.pop(key, None)
        if entry:
            session = entry[2]
            names = [name for name, _, owner in self.tools.values() if owner is session]
            if session.kind == 'S':
                session.display, session.marks, session.last = '\n'.join(names), [], now
                if not names:
                    session.switch('I')
            self.revision += 1

    def highlight(self, spans):
        for session, offset, left, right in self.selected:
            session.marks.extend((offset+max(a, left)-left, offset+min(b, right)-left)
                                 for a, b in spans if a < right and b > left)
        if spans:
            self.revision += 1

    def poll(self, now):
        self.now = now
        if self.socket:
            for _ in range(64):
                try:
                    data = self.socket.recv(65536)
                except BlockingIOError:
                    break
                self.absorb(data, now)
        for key, (_, started, _) in list(self.tools.items()):
            if now-started >= 7200:
                self.end_tool(key, now)
        for key, session in list(self.sessions.items()):
            if session.kind != 'S' and now-session.last >= self.session_timeout:
                del self.sessions[key]
                self.revision += 1
            elif session.kind != 'S' and now-session.last > .3:
                session.flush(now)
        if self.tool_pending and now-self.last_conversion >= 2:
            events = list(self.tool_pending)
            self.tool_pending.clear()
            self.selected, parts, position = [], [], 0
            for session, name in events:
                start = session.display.rfind(name)
                if session.kind == 'S' and start >= 0:
                    self.selected.append((session, session.offset+start, position+1, position+1+len(name)))
                parts.append('*'+name)
                position += len(name)+3
            self.source = 'tool / ' + ', '.join(name for _, name in events)
            self.last_conversion = now
            return '\n\n'.join(parts)
        choices = []
        for (_, _, sid, writer), session in self.sessions.items():
            label = sid + (':'+writer if writer else '')
            priority = PRIORITY.get(session.platform, 0)
            if session.pending:
                kind = 'thinking' if session.kind == 'R' else 'chat'
                source = f"{session.platform or '?'} #{label[-14:]} / {kind}"
                choices.append((max(0, priority-(session.kind == 'R')), session.queued, session, source))
        if not choices or now-self.last_conversion < 2:
            return None
        _, _, channel, self.source = max(choices, key=lambda entry: entry[:2])
        text, channel.pending = channel.pending, ''
        self.selected = [(channel, channel.pending_start, 0, len(text))]
        self.last_conversion = now
        return text

    def deadline(self, now):
        deadlines = [session.last+self.session_timeout for session in self.sessions.values() if session.kind != 'S']
        deadlines.extend(started+7200 for _, started, _ in self.tools.values())
        for session in self.sessions.values():
            if session.buffer.rstrip().endswith(('.', '!', '?', '。', '！', '？')):
                deadlines.append(session.last+.3)
        if self.queued:
            deadlines.append(self.last_conversion+2)
        return min((deadline for deadline in deadlines if deadline > now), default=now+1)

    @property
    def queued(self):
        return bool(self.tool_pending) + sum(bool(s.pending) for s in self.sessions.values())

    @property
    def busy(self):
        return bool(self.tools)

    @property
    def thinking(self):
        return any(s.kind == 'R' and self.now-s.last < self.idle_timeout for s in self.sessions.values())

    def tool_status(self, now):
        return [(name, now-started) for name, started, _ in
                sorted(self.tools.values(), key=lambda entry: entry[1])]

    def debug(self, now):
        for (_, _, sid, writer), session in sorted(self.sessions.items()):
            label = sid + (':'+writer if writer else '')
            kind = {'R': 'thinking', 'T': 'chat', 'S': 'tool', 'I': 'idle'}[session.kind]
            text = session.display
            if session.kind == 'S':
                started = [started for _, started, owner in self.tools.values() if owner is session]
                text += f'\n{now-min(started, default=now):.0f}s'
            yield (f'{session.platform or "?"} #{label[-14:]} / {kind} [{session.count}]', text,
                   33 if session.kind == 'R' else 32 if session.kind == 'T' else 36,
                   [(a-session.offset, b-session.offset) for a, b in session.marks])

    def close(self):
        if self.socket:
            self.socket.close()
            try:
                info = os.stat(self.path)
                if (info.st_dev, info.st_ino) == self.identity:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
            if self.pidfile:
                try:
                    os.unlink(self.pidfile)
                except FileNotFoundError:
                    pass
