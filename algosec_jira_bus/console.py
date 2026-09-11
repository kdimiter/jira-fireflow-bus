"""Small terminal editor shared by the installation and preparation prompts."""
import getpass
import os
import sys
import termios
import unicodedata
import warnings


class ConsoleError(ValueError):
    """Safe error when an interactive credential cannot be read privately."""


def _cell_width(char):
    if unicodedata.combining(char) or unicodedata.category(char) == 'Cf':
        return 0
    return 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1


def _clip(text, columns):
    used = 0
    for end, char in enumerate(text):
        used += _cell_width(char)
        if used > columns:
            return text[:end]
    return text


def prompt(label, *, secret=False):
    """Edit a line with arrows, BS/DEL, Home/End and masked secret rendering."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        if not secret:
            try:
                return input(label)
            except EOFError:
                raise ConsoleError('Input cancelled.') from None
        # getpass can still use the controlling terminal with redirected stdin.
        # Refuse its echoing fallback before it reads any credential.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                return getpass.getpass(label)
            except EOFError:
                raise ConsoleError('Input cancelled.') from None
            except getpass.GetPassWarning:
                raise ConsoleError('An interactive terminal is required for secrets; run with docker -it.') from None

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    edited = termios.tcgetattr(fd)
    # Handle control keys ourselves, so BS and DEL behave identically. Disabling
    # ISIG also lets Ctrl-C restore the terminal before interrupting this prompt.
    edited[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL | termios.ISIG)
    edited[6][termios.VMIN] = 1
    edited[6][termios.VTIME] = 0
    value = []
    cursor = 0
    escape = ''
    view_start = 0

    def redraw():
        nonlocal view_start
        try:
            columns = os.get_terminal_size(sys.stdout.fileno()).columns or 80
        except OSError:
            columns = 80
        # Keep the last terminal cell blank to avoid automatic line wrapping.
        budget = max(0, columns - 1)
        label_budget = max(0, budget - 8)
        shown_label = _clip(label, label_budget)
        room = budget - sum(_cell_width(char) for char in shown_label)
        shown = '*' * len(value) if secret else ''.join(value)
        widths = [1] * len(value) if secret else [_cell_width(char) for char in value]
        view_start = min(view_start, cursor)
        before = sum(widths[view_start:cursor])
        while before > room and view_start < cursor:
            before -= widths[view_start]
            view_start += 1
        end, visible_width = view_start, 0
        while end < len(value) and visible_width + widths[end] <= room:
            visible_width += widths[end]
            end += 1
        sys.stdout.write('\r\x1b[2K' + shown_label + shown[view_start:end])
        tail_width = visible_width - before
        if tail_width:
            sys.stdout.write(f'\x1b[{tail_width}D')
        sys.stdout.flush()

    try:
        termios.tcsetattr(fd, termios.TCSANOW, edited)
        redraw()
        while True:
            char = sys.stdin.read(1)
            if not char or char == '\x04':
                raise ConsoleError('Input cancelled.')
            if char == '\x03':
                raise KeyboardInterrupt
            if char in ('\r', '\n'):
                return ''.join(value)
            if escape:
                if escape == '\x1b':
                    escape = escape + char if char in ('[', 'O') else ''
                    continue
                # Consume the whole CSI/SS3 sequence, including unknown keys.
                # Bound stored parameters while still swallowing its final byte.
                if not ('@' <= char <= '~'):
                    if len(escape) < 32:
                        escape += char
                    continue
                key, escape = escape + char, ''
                if key in ('\x1b[D', '\x1bOD'):
                    cursor = max(0, cursor - 1)
                elif key in ('\x1b[C', '\x1bOC'):
                    cursor = min(len(value), cursor + 1)
                elif key in ('\x1b[H', '\x1bOH', '\x1b[1~'):
                    cursor = 0
                elif key in ('\x1b[F', '\x1bOF', '\x1b[4~'):
                    cursor = len(value)
                elif key == '\x1b[3~':
                    if cursor < len(value):
                        value.pop(cursor)
                else:
                    continue
                redraw()
                continue
            if char == '\x1b':
                escape = char
                continue
            if char == '\x15':
                value.clear()
                cursor = 0
            elif char in ('\x08', '\x7f'):
                if cursor:
                    cursor -= 1
                    value.pop(cursor)
            elif char.isprintable():
                value.insert(cursor, char)
                cursor += 1
            else:
                continue
            redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
        sys.stdout.write('\n')
        sys.stdout.flush()
