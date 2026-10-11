import re
import csv
import gzip
import unicodedata

IDLE = '😴💤🌙🌌🛏✨🌜🌛🌠'
TOOL = '⏳🔧🔨💻📡🛠'
THINK = '🤔💭'
STARTUP = '❤❌🤖'
BOUNDARY = re.compile(r'(?<!\d)\.|\.(?!\d)|[!?。！？—–]|\n\s*\n')


def emoji_chars(text):
    return [char for char in text if not (0xFE00 <= ord(char) <= 0xFE0F
                                          or 0xE0100 <= ord(char) <= 0xE01EF)]


def words(text):
    return tuple(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


class Matcher:
    def __init__(self, path, window=3):
        self.window = window
        self.table = {}
        self.labels, self.rules = {}, {}
        self.rows = []
        with open(path, encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != ['thought', 'emoji']:
                raise ValueError('CSV header must be thought,emoji')
            for row in reader:
                if None in row or not row['thought'] or not row['emoji']:
                    raise ValueError(f'Invalid CSV row {reader.line_num}')
                key = words(row['thought'])
                if not key:
                    raise ValueError(f'CSV row {reader.line_num}: thought must contain words')
                self.labels.setdefault(key, row['thought'])
                chars = [c for c in emoji_chars(row['emoji']) if not c.isspace()]
                self.rows.append((row['thought'], list(dict.fromkeys(chars))))
                existing = self.table.setdefault(key, [])
                existing.extend(c for c in chars if c not in existing)

    @property
    def emojis(self):
        return {char for chars in self.table.values() for char in chars}

    def resolve(self, glyphs):
        for key, chars in self.table.items():
            self.table[key] = [char for char in chars if char in glyphs]

    def __call__(self, text, limit=None):
        result, self.rules, self.spans, offset = [], {}, [], 0
        for fragment in BOUNDARY.split(text):
            offset = text.find(fragment, offset)
            matches = list(re.finditer(r'[^\W_]+(?:[\u0300-\u036f]+[^\W_]*)*', fragment))
            tokens = words(fragment)
            positions = [(offset+match.start(), offset+match.end()) for match in matches for _ in words(match.group())]
            if len(positions) != len(tokens):
                positions = [(offset, offset+len(fragment))]*len(tokens)
            for end in (False, True):
                for size in range(min(len(tokens), self.window), 0, -1):
                    key = tokens[-size:] if end else tokens[:size]
                    if self.table.get(key):
                        chosen = positions[-size:] if end else positions[:size]
                        self.spans.append((chosen[0][0], chosen[-1][1]))
                        for char in self.table[key]:
                            if char not in self.rules:
                                self.rules[char] = self.labels[key]
                                result.append(char)
                        break
            offset += len(fragment)
        return result if limit is None else result[:limit]


def load_font(path, wanted=None):
    glyphs = {}
    opener = gzip.open if str(path).endswith('.gz') else open
    ascent, advance = 14, 16
    with opener(path, 'rt', encoding='ascii') as source:
        for line in source:
            fields = line.split()
            if not fields:
                continue
            if fields[0] == 'FONT_ASCENT':
                ascent = int(fields[1])
            elif fields[0] == 'DWIDTH':
                advance = int(fields[1])
            elif fields[0] == 'STARTCHAR':
                code, width, box, bitmap = -1, advance, None, []
                for line in source:
                    fields = line.split()
                    if not fields:
                        continue
                    tag = fields[0]
                    if tag == 'ENCODING':
                        code = int(fields[1])
                    elif tag == 'DWIDTH':
                        width = int(fields[1])
                    elif tag == 'BBX':
                        box = tuple(map(int, fields[1:]))
                    elif tag == 'BITMAP':
                        for line in source:
                            if line.strip() == 'ENDCHAR':
                                break
                            if code >= 0 and (wanted is None or chr(code) in wanted):
                                bitmap.append(int(line.strip(), 16))
                        break
                    elif tag == 'ENDCHAR':
                        break
                if code < 0 or box is None or not bitmap or not 0 < width <= 16:
                    continue
                bw, bh, bx, by = box
                if len(bitmap) != bh:
                    raise ValueError(f'Invalid BDF bitmap U+{code:04X}')
                rows = [0]*16
                padding = ((bw+7)//8)*8
                for y, bits in enumerate(bitmap):
                    row = ascent-(by+bh)+y
                    if 0 <= row < 16:
                        for x in range(bw):
                            col = bx+x
                            if 0 <= col < width and bits & (1 << (padding-1-x)):
                                rows[row] |= 1 << col
                if any(rows):
                    glyphs[chr(code)] = (width, tuple(rows))
    if not glyphs:
        raise ValueError('No drawable glyphs found in the BDF font')
    return glyphs
