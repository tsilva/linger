"""Character-cell-safe drawing and a small portable terminal palette."""

import curses
import unicodedata


def clean(text: object) -> str:
    return "".join(c if c.isprintable() else "?" for c in str(text))


def cells(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
               for c in clean(text))


def clip(text, width):
    result, used = [], 0
    for c in clean(text):
        size = 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
        if used + size > max(0, width):
            break
        if size or result:
            result.append(c)
        used += size
    return "".join(result)


def fit(text, width, right=False):
    text = clean(text)
    if cells(text) > width:
        text = clip(text, max(0, width - 1)) + ("~" if width else "")
    gap = " " * max(0, width - cells(text))
    return gap + text if right else text + gap


def wrap(text, width):
    """Wrap explanations by terminal cells, retaining wide filenames in full."""
    text = clean(text)
    if width < 1:
        return []
    lines = []
    while cells(text) > width:
        piece = clip(text, width)
        if not piece:
            break
        boundary = piece.rfind(" ")
        if boundary > 0:
            piece = piece[:boundary]
        lines.append(piece)
        text = text[len(piece):].lstrip()
    if text:
        lines.append(text)
    return lines


def palette(screen):
    styles = {name: 0 for name in ("normal", "muted", "unknown", "cyan", "amber", "mint", "purple")}
    styles.update(cyan=curses.A_BOLD, amber=curses.A_BOLD, selected=curses.A_REVERSE)
    if not curses.has_colors():
        return styles
    try:
        curses.start_color()
        if curses.COLORS >= 256:
            bg = 233
            colors = dict(normal=252, muted=245, unknown=146, cyan=81, amber=222, mint=85, purple=183)
        else:
            bg = curses.COLOR_BLACK
            colors = dict(normal=curses.COLOR_WHITE, muted=curses.COLOR_WHITE, unknown=curses.COLOR_WHITE,
                          cyan=curses.COLOR_CYAN, amber=curses.COLOR_YELLOW,
                          mint=curses.COLOR_GREEN, purple=curses.COLOR_MAGENTA)
        for index, (name, fg) in enumerate(colors.items(), 1):
            curses.init_pair(index, fg, bg)
            styles[name] = curses.color_pair(index)
        curses.init_pair(8, curses.COLOR_BLACK, colors["amber"])
        styles["selected"] = curses.color_pair(8) | curses.A_BOLD
        screen.bkgd(" ", styles["normal"])
    except curses.error:
        pass
    return styles


class Canvas:
    def __init__(self, screen, styles, unicode=True):
        self.screen, self.styles, self.unicode = screen, styles, unicode
        self.height, self.width = screen.getmaxyx()

    def put(self, y, x, text, style="normal", width=None, bold=False):
        if y < 0 or y >= self.height or x < 0 or x >= self.width - 1:
            return
        width = min(self.width - x - 1, width if width is not None else self.width)
        attr = self.styles.get(style, 0) | (curses.A_BOLD if bold else 0)
        try:
            self.screen.addstr(y, x, clip(text, width), attr)
        except curses.error:
            pass

    def rule(self, y, x, width, style="cyan"):
        self.put(y, x, ("─" if self.unicode else "-") * max(0, width), style)

    def frame(self, y, height, title, style="cyan", caption=""):
        if height < 3:
            return
        left, right = 1, self.width - 2
        tl, tr, bl, br, vertical = ("╭", "╮", "╰", "╯", "│") if self.unicode else ("+", "+", "+", "+", "|")
        for yy, a, b in ((y, tl, tr), (y + height - 1, bl, br)):
            self.rule(yy, left + 1, right - left - 1, style)
            self.put(yy, left, a, style)
            self.put(yy, right, b, style)
        for yy in range(y + 1, y + height - 1):
            self.put(yy, left, vertical, style)
            self.put(yy, right, vertical, style)
        show_caption = caption and self.width > cells(title) + cells(caption) + 12
        title_width = max(0, self.width - cells(caption) - 10) if show_caption else self.width - 8
        self.put(y, 3, " " + clip(title, title_width) + " ", style, bold=True)
        if show_caption:
            self.put(y, right - cells(caption) - 2, " " + caption + " ", "muted")
