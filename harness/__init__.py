"""harness/ — the unattended layer.

Sits ABOVE calibration. Owns what a night intends to measure, whether each
measurement is usable, and when to stop. Owns no measurement of its own.

    manifest.py   the plan and its outcomes, written before the run
    verdict.py    numeric thresholds — is a cell usable
    adapter.py    the only file that imports calibration
    night.py      the loop

Dependencies run one way, same as everywhere else here:

    harness -> calibration -> control -> detector / press

and never the reverse. `pixi run layering` enforces the part of that a machine
can check.
"""
