def convert(text):
    text = text.casefold()
    return ''.join(emoji for word, emoji in (('but', '❌'), ('actually', '❌'), ('good', '✅'), ('fine', '✅')) if word in text)
