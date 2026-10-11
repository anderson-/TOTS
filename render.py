def render(glyphs, chars, unicode=True, pixel='#', scale=1, repeat=1):
    base = 16*max(1, scale)
    frame = [[0]*(base*len(chars)) for _ in range(base)]
    for index, char in enumerate(chars):
        glyph_width, rows = glyphs.get(char, (16, (0,)*16))
        gx, gy = (base-glyph_width)//2, (base-16)//2
        for y in range(base):
            row, offset = y-gy, index*base
            for x in range(base):
                col = x-gx
                if 0 <= row < 16 and 0 <= col < glyph_width and rows[row] & (1 << col):
                    frame[y][offset+x] = 1
    vertical = 2 if unicode else 1
    lines = []
    for y in range(base//vertical):
        row = []
        for x in range(base*len(chars)):
            if unicode:
                upper = frame[y*2][x]
                lower = frame[y*2+1][x] if y*2+1 < base else 0
                row.append(' ▄▀█'[upper*2+lower])
            elif frame[y][x]:
                cell = pixel*repeat
                row.append(f'\x1b[7m{cell}\x1b[27m')
            else:
                row.append(' '*repeat)
        lines.append(''.join(row))
    return lines
