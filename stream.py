import os
import re
import stat
import socket
from collections import deque
from dataclasses import dataclass, field

PRIORITY = {'telegram': 5, 'cli': 3, 'agent': 2, 'cron': 1}
SENTENCE = re.compile(r'[.!?。！？]+(?=\s|$)')


@dataclass
class Channel:
    buffer: str = ''
    tail: str = ''
    lines: deque = field(default_factory=lambda: deque(maxlen=5))
    pending: str = ''
    queued: float = 0
    count: int = 0

    def feed(self, text, now):
        self.count += len(text.encode('utf-8'))
        self.buffer = (self.buffer+text)[-65536:]
        debug = (self.tail+text.replace('\r', '')).split('\n')
        for line in debug[:-1]:
            self.lines.append(line[-160:])
        self.tail = debug[-1][-160:]
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
        if text.strip():
            self.pending, self.queued = text.strip(), now

    def flush(self, now):
        if self.buffer.rstrip().endswith(('.', '!', '?', '。', '！', '？')):
            self.queue(self.buffer, now)
            self.buffer = ''


@dataclass
class Session:
    platform: str
    last: float
    sequence: int = 0
    chat: Channel = field(default_factory=Channel)
    thinking: Channel = field(default_factory=Channel)


class Store:
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
        self.think = 0

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
        parts = data.decode('utf-8', 'replace').split('\x1f', 6)
        if len(parts) == 7:
            _, platform, sid, kind, writer, sequence, text = parts
        elif len(parts) == 5:
            _, platform, sid, kind, text = parts
            writer = sequence = ''
        elif len(parts) == 2:
            platform, text = parts
            sid = writer = sequence = ''
            kind = 'T'
        else:
            return
        if kind == 'S':
            self.tools[writer] = (text or '', now)
            if text:
                self.tool_pending.append(text)
            self.think = 0
            self.revision += 1
            return
        if kind == 'E':
            self.tools.pop(writer, None)
            self.think = now
            self.revision += 1
            return
        if kind not in ('T', 'R'):
            return
        if kind == 'T':
            self.think = 0
        elif not self.tools:
            self.think = now
        try:
            seq = int(sequence) if sequence else 0
        except ValueError:
            return
        key = (platform, sid, writer)
        session = self.sessions.get(key)
        if session is None:
            session = self.sessions[key] = Session(platform, now)
        if writer and seq:
            if session.sequence and seq != session.sequence+1:
                session.chat.buffer = session.thinking.buffer = ''
                session.chat.pending = session.thinking.pending = ''
            session.sequence = seq
        channel = session.thinking if kind == 'R' else session.chat
        channel.feed(text, now)
        self.total += len(text.encode('utf-8'))
        session.last = now
        self.revision += 1

    def poll(self, now):
        if self.socket:
            for _ in range(64):
                try:
                    data = self.socket.recv(65536)
                except BlockingIOError:
                    break
                self.absorb(data, now)
        if self.tools:
            stale = [key for key, (_, started) in self.tools.items() if now-started >= 7200]
            for key in stale:
                del self.tools[key]
        if self.think and now-self.think >= 90:
            self.think = 0
        for key, session in list(self.sessions.items()):
            if now-session.last >= 90:
                del self.sessions[key]
                self.revision += 1
            elif now-session.last > .3:
                session.chat.flush(now)
                session.thinking.flush(now)
        if self.tool_pending and now-self.last_conversion >= 2:
            names = list(self.tool_pending)
            self.tool_pending.clear()
            self.source = 'tool / ' + ', '.join(names)
            self.last_conversion = now
            return '\n\n'.join('*' + name for name in names)
        choices = []
        for (_, sid, writer), session in self.sessions.items():
            label = sid + (':'+writer if writer else '')
            priority = PRIORITY.get(session.platform, 0)
            for channel, rank in ((session.chat, priority), (session.thinking, max(0, priority-1))):
                if channel.pending:
                    kind = 'thinking' if channel is session.thinking else 'chat'
                    source = f"{session.platform or '?'} #{label[-14:]} / {kind}"
                    choices.append((rank, channel.queued, channel, source))
        if not choices or now-self.last_conversion < 2:
            return None
        _, _, channel, self.source = max(choices, key=lambda entry: entry[:2])
        text, channel.pending = channel.pending, ''
        self.last_conversion = now
        return text

    def deadline(self, now):
        deadlines = [session.last+90 for session in self.sessions.values()]
        for session in self.sessions.values():
            if any(c.buffer.rstrip().endswith(('.', '!', '?', '。', '！', '？'))
                   for c in (session.chat, session.thinking)):
                deadlines.append(session.last+.3)
        if self.queued:
            deadlines.append(self.last_conversion+2)
        return min((deadline for deadline in deadlines if deadline > now), default=now+1)

    @property
    def queued(self):
        return bool(self.tool_pending) + sum(bool(c.pending) for s in self.sessions.values() for c in (s.chat, s.thinking))

    @property
    def busy(self):
        return bool(self.tools)

    @property
    def thinking(self):
        return bool(self.think)

    def tool_status(self, now):
        return [(name, now-started) for name, started in
                sorted(self.tools.values(), key=lambda entry: entry[1])]

    def debug(self):
        for (_, sid, writer), session in sorted(self.sessions.items()):
            label = sid + (':'+writer if writer else '')
            yield f'{session.platform or "?"} #{label[-14:]}', 36
            for name, channel, color in (('chat', session.chat, 32), ('thinking', session.thinking, 33)):
                if name == 'thinking' and not channel.count:
                    continue
                yield f'{name} [{channel.count}]', color
                for line in [*channel.lines, channel.tail]:
                    if line:
                        yield line, color

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
