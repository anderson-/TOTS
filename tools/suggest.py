#!/usr/bin/env python3
"""Suggest reusable Hermes phrases for an editable thought,emoji CSV."""
import csv
import json
import math
import os
import random
import sqlite3
import sys
import shutil
from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path

from matcher import BOUNDARY, words

DB = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'state.db'


def progress(label, done, total):
    if not sys.stderr.isatty():
        return
    width = max(8, min(28, shutil.get_terminal_size().columns - 38))
    fraction = done / total if total else 1
    filled = int(width * fraction)
    bar = '=' * filled + ('>' + '.' * (width - filled - 1) if filled < width else '')
    print(f'\r{label} [{bar}] {done}/{total} {fraction:4.0%}',
          end='\n' if done == total else '', file=sys.stderr, flush=True)


class Stats:
    __slots__ = ('support', 'sessions', 'count', 'balanced', 'contexts', 'score')

    def __init__(self, support):
        self.support, self.sessions, self.count = support, 0, 0
        self.balanced, self.score = 0.0, 0.0
        self.contexts = Counter()


def sources(con, sid):
    for text, raw in con.execute(
            "select reasoning_content,tool_calls from messages "
            "where session_id=? and role='assistant'", (sid,)):
        if text:
            yield 'thought', text
        try:
            calls = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            continue
        for call in calls if isinstance(calls, list) else []:
            function = call.get('function') if isinstance(call, dict) else None
            name = function.get('name') if isinstance(function, dict) else None
            if isinstance(name, str) and name:
                yield 'tool', name


def candidates(kind, text, window, allowed=None):
    for fragment in BOUNDARY.split(text):
        tokens = words(fragment)
        length = min(window, len(tokens))
        for side in ('start', 'end'):
            sizes = (length,) if kind == 'tool' and length else range(1, length + 1)
            for size in sizes:
                gram = tokens[:size] if side == 'start' else tokens[-size:]
                key = kind, side, gram
                if allowed is not None:
                    if key not in allowed:
                        continue
                elif not any(c.isalpha() for token in gram for c in token):
                    continue
                neighbor = None
                if allowed is not None:
                    neighbor = (tokens[size:size+1] if side == 'start' else
                                tokens[len(tokens)-size-1:len(tokens)-size]
                                if size < len(tokens) else ())
                yield key, neighbor


def mine(con, ids, window, threshold, limit):
    total, base = len(ids), len(ids) + 1
    required = max(1, math.ceil(total * threshold / 100))
    support, marginals = {}, defaultdict(Counter)
    progress('Counting', 0, total)
    for done, sid in enumerate(ids, 1):
        remaining = total - done
        for kind, text in sources(con, sid):
            for (_, side, gram), _ in candidates(kind, text, window):
                marginals[(kind, side, len(gram))][gram[-1] if side == 'start' else gram[0]] += 1
                key = ('t' if kind == 'thought' else '*') + ('<' if side == 'start' else '>') + '\x1f'.join(gram)
                packed = support.get(key, 0)
                if not packed and remaining + 1 < required:
                    continue
                count, seen = divmod(packed, base)
                if seen != done:
                    support[key] = (count + 1) * base + done
        if done % max(1, total // 100) == 0 or done == total:
            support = {key: value for key, value in support.items()
                       if value // base + remaining >= required}
        progress('Counting', done, total)
    stats = {('thought' if key[0] == 't' else 'tool',
              'start' if key[1] == '<' else 'end', tuple(key[2:].split('\x1f'))):
             Stats(value // base) for key, value in support.items()}
    del support
    if not stats:
        return []
    progress('Contexts', 0, total)
    for index, sid in enumerate(ids):
        counts = Counter()
        for kind, text in sources(con, sid):
            for key, neighbor in candidates(kind, text, window, stats):
                item = stats[key]
                item.count += 1
                item.sessions |= 1 << index
                item.contexts[neighbor] += 1
                counts[key] += 1
        for key, count in counts.items():
            stats[key].balanced += math.log1p(count)
        progress('Contexts', index + 1, total)
    totals = {key: sum(counter.values()) for key, counter in marginals.items()}
    removed, children = set(), defaultdict(list)
    for key, item in stats.items():
        kind, side, gram = key
        for size in range(1, len(gram)):
            shorter = kind, side, gram[:size] if side == 'start' else gram[-size:]
            parent = stats.get(shorter)
            if parent and item.count >= .85 * parent.count and item.support >= .85 * parent.support:
                removed.add(shorter)
        entropy = -sum((n / item.count) * math.log(n / item.count) for n in item.contexts.values())
        item.score = (item.support * len(gram) * (1 + math.log1p(item.balanced / item.support))
                      * (1 + .1 * min(entropy, 3)))
        if len(gram) > 1:
            parent_key = kind, side, gram[:-1] if side == 'start' else gram[1:]
            children[parent_key].append(key)
            parent = stats.get(parent_key)
            if parent:
                bucket = kind, side, len(gram)
                added = gram[-1] if side == 'start' else gram[0]
                gain = max(0, math.log((item.count / parent.count) /
                                      (marginals[bucket][added] / totals[bucket])))
                item.score *= 1 + .25 * min(gain, 3)
    for key, extensions in children.items():
        if key[0] != 'thought' or key not in stats:
            continue
        count, sessions = 0, 0
        for extension in extensions:
            count += stats[extension].count
            sessions |= stats[extension].sessions
        if count >= .85 * stats[key].count and bin(sessions).count('1') >= .85 * stats[key].support:
            removed.add(key)
    ranked = sorted(stats.keys() - removed, key=lambda key: (-stats[key].score, key))
    thoughts = list(dict.fromkeys(key[2] for key in ranked if key[0] == 'thought'))
    if limit:
        thoughts = thoughts[:limit]
    contained = {gram[i:i+size] for gram in thoughts for size in range(1, len(gram))
                 for i in range(len(gram) - size + 1)}
    labels = [' '.join(gram) for gram in thoughts if gram not in contained]
    labels.extend(dict.fromkeys('*' + ' '.join(key[2]) for key in ranked if key[0] == 'tool'))
    return labels


def random_examples(con, ids, count, window):
    """Sample distinct matcher-sized boundary phrases without mining filters."""
    if not ids:
        return []
    seen, examples = set(), []
    placeholders = ','.join('?' for _ in ids)
    for (text,) in con.execute(
            "select reasoning_content from messages "
            f"where role='assistant' and reasoning_content is not null "
            f"and session_id in ({placeholders})", ids):
        for (_kind, _side, gram), _neighbor in candidates('thought', text, window):
            phrase = ' '.join(gram)
            if phrase not in seen:
                seen.add(phrase)
                examples.append(phrase)
    return random.sample(examples, min(count, len(examples)))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('-n', type=int, default=5, help='sessions to sample; 0 reads all (default: 5)')
    parser.add_argument('--words', type=int, default=3, help='phrase/matcher window (default: 3)')
    parser.add_argument('-x', type=float, default=50, help='minimum session coverage percent (default: 50)')
    parser.add_argument('--limit', type=int, default=50, help='thought limit before pruning; 0 means all (default: 50)')
    parser.add_argument('--examples', type=int, metavar='X',
                        help='print X distinct random extracted reasoning phrases, without mining filters')
    parser.add_argument('-o', default='suggestions.csv', help='output CSV (default: suggestions.csv)')
    args = parser.parse_args()
    if (args.n < 0 or args.words < 1 or args.limit < 0 or
            not 0 <= args.x <= 100 or
            (args.examples is not None and args.examples < 0)):
        parser.error('require n/limit/examples >= 0, words >= 1 and x between 0 and 100')
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    try:
        ids = [row[0] for row in con.execute('select id from sessions order by id')]
        if args.n:
            ids = random.Random(42).sample(ids, min(args.n, len(ids)))
        if args.examples is not None:
            examples = random_examples(con, ids, args.examples, args.words)
        else:
            labels = mine(con, ids, args.words, args.x, args.limit)
    finally:
        con.close()
    if args.examples is not None:
        for example in examples:
            print(example)
        return
    with open(args.o, 'w', encoding='utf-8', newline='') as out:
        writer = csv.writer(out, lineterminator='\n')
        writer.writerow(('thought', 'emoji'))
        writer.writerows((label, '') for label in labels)


if __name__ == '__main__':
    main()
