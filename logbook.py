"""logbook — the terminal is a dashboard, the file is the record.

    pixi run logbook          # offline selftest, no game, no hardware

`print` goes to BOTH. `note` goes to the FILE ONLY. That one distinction is
the whole module, and it exists because the two destinations answer different
questions:

    the terminal   is read WHILE playing, over the game, out of the corner of
                   an eye. Its budget is a few lines. Anything that scrolls
                   the status table off the top has cost more than it carries.
    the file       is read AFTERWARDS, against a burst the reader remembers
                   and cannot place. Its budget is everything.

⚠ THE SPLIT WAS MEASURED, NOT ESTIMATED. Two real play logs, 2026-08-09:

    0809_153510.log   234 lines   154 `[tab]`   31 `[state]`   = 79%
    0809_161910.log   172 lines   116 `[tab]`   25 `[state]`   = 82%

Four fifths of the terminal was two prefixes nobody reads live, and the status
table -- the one thing a player actually looks at -- was 8 lines of the 234,
scrolled away between every pair of them. NOTHING IS BEING DROPPED: every one
of those lines still lands in the file, timestamped, which is where the
question they answer ("why was it not compensating at 15:24") is asked.

⚠ AND `note` FALLS BACK TO `print` WHEN NO LOG IS OPEN. That is not a
convenience, it is the only safe default: with no file there is no record, so
a channel that quietly swallowed its lines would turn "the log could not be
opened" -- one line start_log already prints -- into a whole session with no
evidence anywhere. When the terminal IS the record, everything goes to it.

⚠ IT IS LAYER-LESS, like config.py and daemon_loop.py, and it has to stay that
way: it imports nothing from this repository, so any layer may call it without
creating an edge. See tools/check_layering.py's docstring for what the repo
root admits.
"""
import datetime
import os
import sys


class _Tee:
    """Write to the terminal AND to a timestamped file. Never swallows the
    terminal.

    ⚠ THE FILE IS THE COPY, NOT THE DESTINATION. If the log file cannot be
    opened or a write to it raises, the terminal write has already happened
    and the failure is dropped -- a broken log must not be able to take down
    a session that is otherwise fine, and least of all one that is driving
    hardware.

    ⚠ THE CLOCK GOES IN THE FILE ONLY, and that asymmetry is the point. The
    terminal is read LIVE, where the order is the timing and a column of
    timestamps is noise. The file is read AFTERWARDS, against a burst the
    reader remembers and cannot place: without a clock, `[armed] CLEARED` and
    a burst three seconds later are one line apart and look simultaneous.
    Every line this process prints is a state CHANGE, so the gaps between them
    carry as much as the lines do -- and they were being thrown away.

    Prefixing is per LINE, not per write: `print` issues the text and the
    newline as two calls, and stamping both would put a bare timestamp on the
    end of every line.
    """

    def __init__(self, stream, fh):
        self._stream, self._fh = stream, fh
        self._at_line_start = True

    def to_file(self, s):
        """The file half, on its own. This is what `note` writes through.

        ⚠ IT SHARES `_at_line_start` WITH `write`, AND IT HAS TO. The two
        channels interleave in one file, so a `note` issued while a `print`
        has emitted its text but not yet its newline must NOT stamp a clock in
        the middle of that line. One flag, one file, one notion of where the
        line starts -- keeping a second one here is how the log would grow
        timestamps in its middles.
        """
        try:
            for part in s.splitlines(keepends=True):
                if self._at_line_start and part.strip():
                    self._fh.write(datetime.datetime.now().strftime(
                        '%H:%M:%S.%f')[:-3] + ' ')
                self._fh.write(part)
                self._at_line_start = part.endswith('\n')
            self._fh.flush()
        except (OSError, ValueError):
            pass

    def write(self, s):
        n = self._stream.write(s)
        self.to_file(s)
        return n

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def note(msg):
    """One line for the LOG FILE only. Falls back to print when there is none.

    Use it for what a reader wants AFTERWARDS and never during: every Tab
    edge, every `stop_recoil` flip, every frame saved to disk. Keep `print`
    for what changes what the player should do right now -- the status table,
    a curve that could not be found, a failure.

    ⚠ IT ASKS `sys.stdout`, IT DOES NOT CACHE A HANDLE. start_log replaces
    sys.stdout, and something else may replace it again (a test harness, a
    second start_log, a redirect). Caching the file handle here would keep
    writing into whichever log happened to be open first, which is the same
    "the record describes a different object than the one measured" failure
    this repository keeps paying for -- only about its own log.
    """
    out = sys.stdout
    if isinstance(out, _Tee):
        out.to_file(f'{msg}\n')
    else:
        print(msg, flush=True)


def start_log(root=None):
    """Tee stdout+stderr into calibration/artifacts/robot/<stamp>.log.

    -> the path, or None if it could not be opened. One file per run, because
    the question a log answers here is always "what happened in THAT session".
    """
    root = root or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'calibration', 'artifacts', 'robot')
    try:
        os.makedirs(root, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%m%d_%H%M%S')
        path = os.path.join(root, f'{stamp}.log')
        fh = open(path, 'a', encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'[log] cannot open a log file ({e}) -- terminal only',
              flush=True)
        return None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    print(f'[log] {path}', flush=True)
    return path


def _selftest():
    """Both channels, against a fake terminal and a fake file.

    ⚠ THE CASES THAT MATTER ARE THE TWO NEGATIVES: `note` must not reach the
    terminal, and it must not lose the line when there is no file. A test that
    only checked "note wrote something somewhere" passes under both bugs.
    """
    import io

    fails = []

    def check(name, got, want):
        ok = got == want
        print(f'  {"ok  " if ok else "FAIL"}  {name:<46} {got!r}'
              + ('' if ok else f'   want {want!r}'))
        if not ok:
            fails.append(name)

    term, fh = io.StringIO(), io.StringIO()
    tee = _Tee(term, fh)
    saved, sys.stdout = sys.stdout, tee
    try:
        print('visible', flush=True)
        note('quiet')
        print('visible again', flush=True)
    finally:
        sys.stdout = saved

    t_lines = term.getvalue().splitlines()
    f_lines = fh.getvalue().splitlines()
    check('the terminal got both prints', t_lines, ['visible', 'visible again'])
    check('the terminal did NOT get the note',
          any('quiet' in ln for ln in t_lines), False)
    check('the file got all three', len(f_lines), 3)
    check('the note is in the file, in order',
          [ln.split(' ', 1)[1] for ln in f_lines],
          ['visible', 'quiet', 'visible again'])
    check('every file line is stamped',
          all(len(ln.split(' ', 1)[0]) == 12 and ln[2] == ':'
              for ln in f_lines), True)
    check('the terminal is NOT stamped',
          any(':' in ln for ln in t_lines), False)

    # ⚠ A note landing mid-line must not stamp a clock into the middle of it.
    # `print` emits the text and the newline as two separate writes, so this
    # is the ordinary case, not a contrived one.
    term2, fh2 = io.StringIO(), io.StringIO()
    tee2 = _Tee(term2, fh2)
    tee2.write('half a line')
    tee2.to_file('')             # a note with nothing in it, worst case
    tee2.write('\n')
    check('a write split across calls is stamped once',
          fh2.getvalue().count(':'), 2)

    # No Tee at all: the terminal is the record, so nothing may be dropped.
    term3 = io.StringIO()
    saved, sys.stdout = sys.stdout, term3
    try:
        note('nowhere to hide')
    finally:
        sys.stdout = saved
    check('with no log file, note PRINTS', term3.getvalue().strip(),
          'nowhere to hide')

    print()
    if fails:
        for f in fails:
            print(f'  FAIL  {f}')
        return 1
    print('all ok')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    sys.exit(_selftest())
