"""The lifecycle every control/ driver has, in one place.

    class SpawnerControl(Driver):
        def __init__(self, ...):
            super().__init__()
            self.screen = SpawnerDetector()

        def close(self):
            super().close()          # releases the Pointer
            ...anything of its own...

⚠ THIS IS NOT ABOUT SAVING FIFTY LINES. Four classes in this layer had four
different answers to the same three questions, and the differences were
invisible from a call site — which is the failure this whole layer's level
discipline exists to prevent. Measured 2026-08-07:

                       lazy Pointer   close()   `with` releases anything
    LobbyControl            yes         yes              yes
    MapControl              yes         yes              yes
    SpawnerControl          NO          NONE             NO   (__exit__: pass)
    InventoryControl        NO          yes              no `with` at all

The two that are wrong are the two that matter most: SpawnerControl has twelve
`with` call sites, all of them reading like resource management and none of
them releasing a byte, and InventoryControl has forty-one construction sites
each hand-rolling its own try/finally.

TWO THINGS THIS FIXES THAT ARE NOT STYLE:

**The Pointer is lazy, and control/CLAUDE.md has said so for months** -- "只读
状态的调用方不会去占串口。别在构造函数里提前建它". Both offenders built one in
__init__, so merely CONSTRUCTING them opened COM10:

    >>> sc = SpawnerControl(verbose=False)
    [pico] connected on COM10

That is the shared serial port this repo warns about on every page, taken by
`sc.plan()` -- the path documented as 纯离线, "想先看它要点哪，不碰游戏" -- and
by every `ensure_ready()` call, which builds a SpawnerControl to ask whether a
panel is shut. Rule 11 in tools/check_layering.py keeps it from coming back.

**close() RELEASES THE POINTER, IT DOES NOT CLOSE THE MOUSE.** press.pico_mouse
.get_mouse() is a process singleton shared with whatever else is running, and
several agents share one Pico. Closing it here would reach outside this
object's lifetime and take the device away from somebody else's run. Dropping
the reference is the whole of it; the next `self.pointer` builds another
handle onto the same singleton for free. MapControl already did exactly this
and its comment is the reason this note exists.
"""
from press.pointer import Pointer


class Driver:
    """Lazy Pointer, uniform close, working `with`."""

    def __init__(self):
        # ⚠ THE `backend` PARAMETER IS GONE (2026-08-08). It chose between the
        # Pico and a SendInput backend, and it threaded through all four
        # subclasses and four --backend CLI flags to reach one Pointer() call.
        # PUBG reads raw HID, so the alternative it selected could not click,
        # turn or fire; deleting it deleted the parameter's only two values
        # that differed.
        self._pointer = None

    @property
    def pointer(self):
        """The Pointer, built on first use.

        ⚠ TOUCHING THIS OPENS THE SERIAL PORT. That is the entire reason it is
        a property and not an attribute: a caller that only reads state, plans
        a click list or asks what the panel looks like never reaches it, and
        so never takes the device from another agent.
        """
        if self._pointer is None:
            self._pointer = Pointer()
        return self._pointer

    def can_press(self):
        """Can this drive the game at all? Answered in the first second.

        Every screen in this layer is opened by a keypress, so a False here
        means the run is impossible -- and knowing that up front rather than
        four minutes in is the whole point.

        It was written twice, verbatim, on InventoryControl and SpawnerControl,
        and the second copy's docstring said "Same method, same reason, as
        SpawnerControl.can_press". Noting a duplicate in a comment registers it
        rather than removing it.

        ⚠ It DOES build the Pointer, unavoidably: asking whether the device is
        there means opening it. Which is also why it belongs on this layer --
        calibration/ used to answer the question by constructing a throwaway
        Pointer just to read `.pico`, importing press/ from a module that is
        not supposed to know devices exist.

        ⚠ IT USED TO READ `self.pointer.pico is not None`, which stopped being
        a question on 2026-08-08: with the SendInput backend gone, a Pointer
        that constructs at all HAS a Pico, and one that cannot raises. So the
        check moved into the except -- and the reason is PRINTED rather than
        swallowed, because "another agent is holding COM10" and "no cable" are
        the same False and want opposite responses from the operator.
        """
        try:
            # `return self.pointer is not None`, not a bare `self.pointer`
            # statement: the bare form is what this check used to be, and
            # ruff B018 / autoflake delete it as a no-op expression -- which
            # would leave the try/except wrapping nothing and the gate always
            # True. The property call IS the check; make it load-bearing.
            return self.pointer is not None
        except Exception as e:
            print(f'[driver] no usable Pico: {e}', flush=True)
            return False

    def close(self):
        """Release this object's handle on the device. Safe to call twice.

        NOT the device itself -- see the module docstring. Subclasses override
        and call super().close() first, then release what they own.
        """
        self._pointer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        # None, not False: same meaning to the interpreter, but `return False`
        # is what SpawnerControl.__exit__ said while releasing nothing, and it
        # read like a deliberate "do not suppress" rather than an empty body.
        return None
