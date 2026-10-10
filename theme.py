THEME_ITEMS = ('bg', 'fg', 'info', 'debug', 'thought', 'output')
DEFAULT_THEME = 'bgfgwhcygnyl'
COLOR_NAMES = ('bk', 'rd', 'gn', 'yl', 'bl', 'mg', 'cy', 'wh')
FG_COLORS = (39, *range(30, 38), *range(90, 98))
BG_COLORS = (49, *range(40, 48), *range(100, 108))
PRESETS = (
    ('terminal', DEFAULT_THEME),
    ('classic', 'bkwh+whcygnyl'),
    ('crimson', 'bkrd+ylcygnyl'),
    ('amber', 'bkyl+ylcygnyl'),
    ('green', 'bkgn+gncygnyl'),
    ('cobalt', 'blwh+whcy+gn+yl+'),
    ('paper', 'whbkbkblgnrd'),
    ('dusk', 'blmg+whcygnyl'),
)


def color_code(token, background=False):
    bright = token.endswith('+')
    name = token[:-1] if bright else token
    if name in ('fg', 'bg'):
        if bright:
            raise ValueError(f'{name}+ is not a color')
        if (name == 'fg') == background:
            raise ValueError(f'{name} cannot be used as the background' if background
                             else f'{name} cannot be used as a foreground')
        return 49 if name == 'bg' else 39
    if name not in COLOR_NAMES:
        raise ValueError(f'unknown color {token!r}')
    return (40 if background else 30) + (60 if bright else 0) + COLOR_NAMES.index(name)


def color_name(code, background=False):
    if code == (49 if background else 39):
        return 'bg' if background else 'fg'
    bright = code >= (100 if background else 90)
    base = (100 if background else 90) if bright else (40 if background else 30)
    return COLOR_NAMES[code-base] + ('+' if bright else '')


def theme_string(theme):
    return ''.join(color_name(code, index == 0) for index, code in enumerate(theme))


def parse_theme(value):
    value, tokens, index = value.strip().lower(), [], 0
    while index < len(value):
        token, index = value[index:index+2], index+2
        if index < len(value) and value[index] == '+':
            token, index = token+'+', index+1
        tokens.append(token)
    if len(tokens) != 6:
        raise ValueError('theme needs six colors: bg fg info debug thought output')
    return tuple(color_code(token, position == 0) for position, token in enumerate(tokens))
