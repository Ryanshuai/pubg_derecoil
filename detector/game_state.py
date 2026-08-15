"""GameState — pure game state + mutation methods.

No key dispatch, no detection scheduling, no hardware communication.
Just state and methods to change it.
"""
from logbook import note
from detector.weapon import Weapon


class GameState:
    def __init__(self):
        # ── Weapon ──
        self.weapon_1 = Weapon()
        self.weapon_2 = Weapon()
        self.active = self.weapon_1

        # ── GT / Pred (int: 0=unknown, 1=slot1, 2=slot2; tuple: weapon names) ──
        self.weapon_gt = ('', '')         # from Tab scan, e.g. ('akm', 'm416')
        self.weapon_pred = ('', '')       # from DL weapon_hud

        # ── Is there a gun in that rack slot at all? ONLY Tab may answer ──
        #
        # None = nobody has looked, True/False = the last Tab read said so.
        # `set_rack` is the only author; see it for why the HUD may not be one.
        self.weapon_present = {1: None, 2: None}
        # Has a Tab read ever come back with a rack? Until it has, the HUD
        # naming is all there is; after it has, the HUD is not consulted again.
        self.rack_seen = False
        # ⚠ `highlight_gt` IS NOT A SECOND OPINION ABOUT THE ACTIVE SLOT. It is
        # "the operator asserted a slot recently and nothing since could have
        # changed it", and its only consumer is calibration/mismatch.py, which
        # needs to know whether it has a truth to compare a prediction against.
        # The keys that zero it (f, g, x, tab) are saying "GT is stale", not
        # "ask the screen instead" -- there is nothing to ask any more.
        self.highlight_gt = 0           # from key 1/2
        self.highlight_gt_ts = 0.0      # when highlight_gt was last set
        self.attachments = {}           # from Tab scan

        # ── Derived state ──
        self.fire_mode = ''
        self.posture = 'standing'

        # Last line print_status printed, so an unchanged state prints once
        # rather than every tick. Declared here instead of springing into
        # existence on first call: `getattr(self, '_last_status', '')` was a
        # note saying this object had no settled shape, and the loop that
        # reads it runs on every keypress.
        self._last_status = ''

        # ── Flags ──
        self.stop_recoil = False
        self.tab_open = False
        self.aim_enabled = False

        # ── Head lead, live-adjustable from the arrow keys ──
        #
        # ⚠ config SEEDS THIS AND THEN STOPS BEING THE AUTHOR. Once the arrow
        # keys can move it, "what lead is the firmware playing" has exactly one
        # runtime answer and it is this attribute -- control/match.py writes it
        # onto the mouse before every upload. config.RECOIL_HEAD_LEAD_MS is the
        # value the session STARTS at, which is a different claim, and the two
        # must not both be read as "the current lead".
        import config
        self.lead_ms = float(config.RECOIL_HEAD_LEAD_MS)

    # ════════════════════════════════════════════════════════════
    # Active weapon — ONE source: the 1 / 2 keys
    # ════════════════════════════════════════════════════════════

    def set_active_by_key(self, slot):
        """Key 1/2 pressed. The ONLY thing that moves `active`.

        ⚠ THE HUD HIGHLIGHT USED TO BE A SECOND AUTHOR, and it moved the焦点
        onto a gun the player was not holding. `set_active_by_detect` wrote
        `active` from HighlightDetector whenever `highlight_gt` was 0 -- and it
        is 0 after every f / g / x / **tab**, which on the calibration path is
        most of the time. Measured on the 2026-08-10 play log, akm+scar and
        p90+mp5k: the table flipped its `*` between the two guns with no key
        touched, and the mismatch collector printed the reason on the very next
        line -- `gt_int=1 pred_int=2`, the detector reading slot 2 while the
        operator had pressed 1.

        Which gun is in the hands decides which curve the firmware plays, so a
        wrong `active` is a whole magazine compensated with the other gun's
        pattern. **A keypress is a statement by the operator; the highlight is
        a guess about a HUD.** They are not two opinions of equal standing, and
        the file already says this about `tab_open` twenty lines below: 一个
        猜出来的状态和一个看过屏幕的状态不该混在同一个字段里 -- except this
        one failed in the other direction, guessing when it had been told.

        HighlightDetector is untouched and still scored (`pixi run highlight`,
        254/254) and still collects training data through
        calibration/mismatch.py. What it no longer does is DRIVE anything.

        ⚠ WHAT THIS GIVES UP, and it is real: picking a weapon up with F while
        empty-handed auto-equips it, and the mouse wheel switches guns too.
        Neither presses 1 or 2, so `active` stays where the operator last put
        it until they press a number key. That is a stale reading; the thing it
        replaces was a wrong one.
        """
        import time
        # ⚠ AN EMPTY SLOT REFUSES THE KEY, AND THE GAME IS THE ONE REFUSING.
        # PUBG does not switch to a rack slot with no gun in it: the press is
        # swallowed and the weapon already in the hands stays there. Moving
        # `active` onto the empty Weapon anyway made the press mean "stop
        # compensating" -- that gun has no name, so no curve, so the firmware
        # was disarmed for a gun the player is still holding and still firing.
        #
        # This is the ONLY thing allowed to refuse a keypress here, and it can
        # only do it on a MEASUREMENT: `weapon_present` is False only when the
        # rack tag said so (set_rack). `None` -- nobody has looked -- goes
        # through, because refusing on ignorance would strand the operator
        # before the first Tab of the session.
        if self.weapon_present.get(slot) is False:
            note(f'[state] key {slot} refused: the rack slot holds no gun, so '
                 f'the game keeps {self.active.name or "(empty)"} in hand. '
                 f'Moving focus here would disarm a gun that is still out.')
            return
        self.active = self.weapon_1 if slot == 1 else self.weapon_2
        self.highlight_gt = slot
        self.highlight_gt_ts = time.perf_counter()

    def forget_rack(self):
        """A pickup happened: what the rack holds is no longer known.

        ⚠ WITHOUT THIS, `set_active_by_key`'s refusal above outlives its
        evidence. Pick a gun up into the empty slot 2 and press 2: the game
        switches, and a stale `present[2] is False` would refuse the switch and
        keep compensating with slot 1's curve -- a WRONG curve on a gun in the
        hands, which is worse than the no-curve state the refusal exists to
        prevent. F is the event that can change occupancy, so F is where the
        reading expires.

        ⚠ IT DOES NOT CLEAR `rack_seen`. That flag says "the HUD has had its
        turn" and a pickup does not un-open the Tab panel; conflating the two
        would put the 47%-phantom reader back in charge (see `weapon_name`).
        Forgetting occupancy and re-enabling a bad reader are different acts.
        """
        self.weapon_present = {1: None, 2: None}

    # ════════════════════════════════════════════════════════════
    # Weapon name — two sources
    # ════════════════════════════════════════════════════════════

    def set_rack(self, present):
        """A Tab read came back. `present`: {1: bool, 2: bool}.

        ⚠ THIS IS THE ONLY PLACE THAT MAY SAY A SLOT IS EMPTY, and the HUD
        detector is deliberately not allowed to. An empty weapon slot in the
        bottom-right HUD draws NOTHING — the crop is the game world showing
        through — so `WeaponHudDetector.drawn`, whose floor was fitted on
        unlabelled plates, cannot refuse it. Measured on the 867 frames in
        calibration/artifacts/ads/runs where slot 1 holds a kar98k and slot 2
        is empty throughout:

            slot 2 (EMPTY)   Laplacian median 317 against a floor of 12
                             drawn() says "something is here"        75.5%
                             the ranking then returns a confident name  47%
                             (aug 347, kar98k 40, mg3 6, m762 3, ...)

        Grass has more high-frequency detail than an icon does, so no floor on
        that quantity separates the two. The rack tag does: it is painted only
        when the panel is up AND that slot holds a gun (detector/
        gun_tag_detector.py, 38 frames, both thresholds mid-gap).

        ⚠ AND THE NEGATIVE IS ONLY TRUSTED BECAUSE THE OTHER SLOT'S POSITIVE
        CAME OFF THE SAME FRAME. control/tab_watch.py calls this only when at
        least one tag was drawn, which is what proves the panel was up; a tag
        that is absent because there is no panel says nothing about the rack.
        """
        self.weapon_present = {s: present.get(s) for s in (1, 2)}
        self.rack_seen = True

    @property
    def weapon_name(self):
        """Effective weapon names: an empty rack slot > GT > pred > existing.

        ⚠ `''` USED TO MEAN BOTH "nothing there" AND "could not read it", and
        the `or` chain resolved both to the oldest guess in the system. Play
        log 2026-08-15 13:06:50, two lines apart:

            [tab] read ... | gun2 (no gun in the rack slot)
                m416 | full ... | 无曲线          <- slot 2, on the next line

        The rack had answered correctly and the answer was thrown away. So
        "nothing there" is now a THIRD state, carried in `weapon_present`, and
        it beats every name — including a name this object is still holding.

        ⚠ AND THE HUD IS ONLY CONSULTED BEFORE THE FIRST TAB READ. That is the
        operator's rule and it is the right way round: at the landing there is
        no rack reading yet and a rough name is better than none, but once Tab
        has answered once, the HUD's 47%-phantom rate is pure downside — it
        cannot see attachments, so nothing it says can add a curve, while
        anything it gets wrong renames a gun and clears the kit. Same shape as
        HighlightDetector: still read, still collected, no longer driving.

        ⚠ WHAT THIS GIVES UP, and it is real: a gun picked up AFTER the first
        Tab read is not renamed until the next one, so that burst fires the
        previous gun's curve. The alternative was measured on the same log --
        13:07:07 the HUD renamed slot 2 `m762 -> vss` and 3 s later back to
        `m762`, neither read prompted by a pickup.
        """
        return tuple(self._effective(s) for s in (1, 2))

    def _effective(self, slot):
        if self.weapon_present.get(slot) is False:
            return ''
        i = slot - 1
        if self.weapon_gt[i]:
            return self.weapon_gt[i]
        if not self.rack_seen and self.weapon_pred[i]:
            return self.weapon_pred[i]
        return (self.weapon_1 if slot == 1 else self.weapon_2).name

    def sync_weapons(self):
        """Apply effective weapon names to Weapon objects. Call after gt/pred change.

        ⚠ AND THIS IS WHERE A KIT DIES, because it is where a gun is OBSERVED
        to have become a different gun. Clearing the attachments used to hang
        off the F key instead -- every pickup wiped both guns' scope, muzzle,
        grip and stock, on the reasoning that picking a weapon up replaces
        what it wears.

        F is the most-pressed key in a real match (ammo, meds, attachments,
        armour) and almost none of those presses change your gun. Nothing
        re-read the kit afterwards either, because attachments are only
        visible on the Tab panel -- so ONE pickup dropped the curve key to
        `bare` and the compensation stayed off until the player happened to
        open Tab. Measured in a play log 2026-08-09: 30 bursts, `[armed]`
        printed ONCE, and four m416 bursts went down recorded as `bare`.

        Clearing on a KEYPRESS is a guess about what the world did. Clearing
        on an observed NAME CHANGE is a measurement of it, and the name is
        already read 500 ms after every F.

        ⚠ WHAT THIS GIVES UP, and it is real: picking an ATTACHMENT up with F
        auto-fits it without changing the weapon name, so that burst fires the
        previous kit's curve. The error is that one part's factor -- a
        compensator is ~0.72, so ~39% over-compensated -- against 100% and no
        compensation at all before. Better, and in the other direction: the
        crosshair is pushed down rather than left to climb.
        """
        w1, w2 = self.weapon_name
        for slot, name, w in [(1, w1, self.weapon_1), (2, w2, self.weapon_2)]:
            if name == w.name:
                continue
            was = w.name
            w.set('name', name)
            self.clear_attachments(slot)
            # To the FILE: the status table shows the consequence -- the
            # new name with a row of `-` where the kit was -- on the very
            # next print, and it shows it more legibly than a sentence
            # does. What the file keeps is WHEN, and what it was before.
            #
            # ⚠ THE EMPTY CASE IS REACHABLE FROM EXACTLY ONE PLACE: the rack
            # said this slot holds nothing (`weapon_name` explains why nothing
            # else may say it). Without this branch a name could only ever be
            # REPLACED, never removed, so a gun that was dropped stayed on the
            # screen and kept its curve -- and pressing its number armed the
            # firmware with a weapon that is not in the hands.
            if not name:
                note(f'[state] gun {slot}: {was} -> (empty), curve cleared '
                     f'(the rack slot holds no gun)')
            else:
                note(f'[state] gun {slot}: {was or "(empty)"} -> {name}, '
                     f'kit cleared (a different weapon wears different '
                     f'parts; Tab will read the new one)')
            w.set_seq()

    # ════════════════════════════════════════════════════════════
    # Fire mode / Posture
    # ════════════════════════════════════════════════════════════

    def set_fire_mode(self, mode):
        self.fire_mode = mode
        self.active.set('fire_mode', mode)
        self.active.set_seq()

    def set_posture(self, posture):
        if posture not in ('standing', 'crouching', 'prone'):
            return
        self.posture = posture
        for w in (self.weapon_1, self.weapon_2):
            w.set('posture', posture)
            w.set_seq()

    # ════════════════════════════════════════════════════════════
    # Attachments
    # ════════════════════════════════════════════════════════════

    _SLOT_TO_ATTR = {'scope': 'scope', 'muzzle': 'muzzle',
                     'grip': 'grip', 'stock': 'butt'}

    def set_attachments(self, slot, attachments):
        """attachments: dict {scope, muzzle, grip, magazine, stock} → class name or ''."""
        from detector.weapon_attachments import validate_attachments
        w = self.weapon_1 if slot == 1 else self.weapon_2
        filtered = validate_attachments(w.name, attachments)
        for slot_name, val in filtered.items():
            attr = self._SLOT_TO_ATTR.get(slot_name)
            if attr:
                w.set(attr, val)
        w.set_seq()

    def clear_attachments(self, slot=None):
        """Forget what a gun wears. `slot` 1 or 2, or None for both.

        ⚠ PER GUN BY DEFAULT NOW. It cleared BOTH unconditionally and hung off
        the F key; sync_weapons explains why that is wrong and what replaced
        it. Wiping the gun you are not holding is a second thing the caller
        did not ask for -- the slot you swapped is the slot whose kit changed.
        """
        self.attachments = {}
        guns = ((self.weapon_1, self.weapon_2) if slot is None
                else (self.weapon_1 if slot == 1 else self.weapon_2,))
        for w in guns:
            for attr in ('scope', 'muzzle', 'grip', 'butt'):
                w.set(attr, '')
            # ⚠ AFTER THE LOOP, BECAUSE `set()` MEANS "I AM TELLING YOU" AND
            # THIS MEANS "I NO LONGER KNOW". Weapon.set marks kit_seen on every
            # kit slot -- `build_weapon` passing '' is a statement that the gun
            # is bare and must be believed -- so clearing through set() would
            # leave the gun claiming an OBSERVED bare kit. That is the state
            # Weapon.kit_seen exists to keep distinguishable: sync_weapons calls
            # this the moment a gun becomes a different gun, which is precisely
            # when nobody has looked at the new one yet.
            w.kit_seen = False
            w.set_seq()

    # ════════════════════════════════════════════════════════════
    # Scale adjust
    # ════════════════════════════════════════════════════════════

    def adjust_counts(self, delta):
        import config
        if self.active.type == 'sp':
            config.COUNTS_PER_PIXEL = max(0.01, round(config.COUNTS_PER_PIXEL + delta, 3))
            print(f"[aim scale] COUNTS_PER_PIXEL = {config.COUNTS_PER_PIXEL:.3f}", flush=True)
        else:
            self.active.adjust_scale(delta)
            name = self.active.name or '(empty)'
            if self.posture == 'standing':
                print(f"[scale] {name} = {self.active.scale:.3f}", flush=True)
            else:
                pf = self.active.get_posture_factor()
                print(f"[posture] {name} {self.posture} = {pf:.3f}", flush=True)

    # ════════════════════════════════════════════════════════════
    # Head-lead adjust
    # ════════════════════════════════════════════════════════════

    def adjust_lead(self, delta):
        """Nudge the head lead by `delta` ms and say what it actually did.

        ⚠ THE PRINT IS THE FEATURE, NOT THE NUDGE. `upload_pattern` folds every
        knot before t=0 into one step at t=0, and the curves are on a 17 ms
        grid -- so the lead only changes anything when it crosses a knot. About
        16 of every 17 presses move the delivered step by EXACTLY ZERO, and a
        control that silently does nothing is indistinguishable from a
        parameter that does not matter. This is the root CLAUDE.md's
        "判据必须能看见它要管的那个维度" in a key binding: the visible number
        must be the one being changed (counts at the click), not the one being
        typed (milliseconds).

        So every press prints the step in counts, how many knots are folded,
        and how far the NEXT boundary is -- which is the only number that tells
        the operator how many more times to press.
        """
        before, _, _ = self._fold_step()
        self.lead_ms = max(0.0, round(self.lead_ms + delta, 1))
        step, n_knots, to_next = self._fold_step()
        import config
        d = -(config.RECOIL_COMP_LAG_MS + self.lead_ms)
        moved = '  <-- CHANGED' if before != step else ''
        up, dn = to_next
        nxt = ('  next change: '
               + ('-- ' if up is None else f'{up:.0f}> ')
               + ('--' if dn is None else f'<{dn:.0f}'))
        # ⚠ note() AND NOT print(), UNLIKE adjust_counts ABOVE. The scale keys
        # print to the console only, and on 2026-08-10 that cost an attribution:
        # the lead was nudged on a VSS, every rifle went soft on its FIRST SHOT
        # -- because this value is GLOBAL and the rifles came back at whatever
        # the VSS had been left at -- and the log could not say what it had been
        # left at. A magazine whose lead is not in the log is a magazine that
        # cannot be attributed afterwards, which is this repo's second
        # cross-layer law with a keypress in it.
        note(f'[lead] {self.active.name or "(empty)"} {self.lead_ms:+.0f} ms '
             f'head  (offset {d:+.0f} ms)  step {step:6.2f} counts  '
             f'{n_knots} knots folded{nxt}{moved}')

    def _fold_step(self):
        """(counts at t=0, knots folded, (presses right, presses left)).

        ⚠ THE PAIR IS THE POINT, NOT ONE NUMBER. The keys are symmetric and
        the boundaries are not: standing just past a knot, one press left
        undoes it and seventeen presses right are needed to gain the next. One
        distance answers only one of those and the operator cannot tell which
        one they were given.

        Mirrors press/pico_mouse.upload_pattern's fold. It is a mirror and not
        an import because this layer must not reach for the hardware -- the
        layering rule that keeps detector/ runnable on a stored PNG. The thing
        that keeps the two honest is that both read the SAME curve off
        `Weapon`, so a mismatch shows up as the printed step disagreeing with
        what the gun does, not as a silent divergence.
        """
        import config
        w = self.active
        t_s, dy_s = list(w.t_s), list(w.dy_s)
        if not t_s:
            return 0.0, 0, (None, None)
        cut = (config.RECOIL_COMP_LAG_MS + self.lead_ms) / 1000.0
        step, n, last = 0.0, 0, None
        for t, dy in zip(t_s, dy_s):
            if t < cut:
                step += float(dy)
                n += 1
                last = t
            else:
                # right = more lead, so the next knot ABOVE the cut; left =
                # less, so back past the last knot BELOW it. Both in presses,
                # which is what ±1 ms per press makes them.
                back = None if last is None else (cut - last) * 1000.0
                return step, n, ((t - cut) * 1000.0, back)
        back = None if last is None else (cut - last) * 1000.0
        return step, n, (None, back)

    # ════════════════════════════════════════════════════════════
    # Aim toggle
    # ════════════════════════════════════════════════════════════

    # tab_open is written by control/tab_watch.py, from the screen. There used
    # to be a toggle_tab_open() here that flipped it on the keypress; it is
    # gone on purpose. Inferring a screen state from "I saw the key that asks
    # for it" is the thing this codebase keeps getting bitten by -- the key
    # can be swallowed, and the screen can change with no key at all.

    def toggle_aim(self):
        self.aim_enabled = not self.aim_enabled
        print(f"[aim] {'ON' if self.aim_enabled else 'OFF'}", flush=True)


    # ════════════════════════════════════════════════════════════
    # Display
    # ════════════════════════════════════════════════════════════

    def print_status(self):
        l1 = self._fmt(self.weapon_1, self.weapon_1 is self.active)
        l2 = self._fmt(self.weapon_2, self.weapon_2 is self.active)
        new = f'{l1}\n{l2}'
        if new != self._last_status:
            self._last_status = new
            print(f'--------------------------------------\n{new}', flush=True)

    _ATTACH_CN = {
        'Upper_DotSight_01_C': '1x', 'Upper_Holosight_C': '1x',
        'Upper_Aimpoint_C': '2x', 'Upper_Scope3x_C': '3x',
        'Upper_ACOG_01_C': '4x', 'Upper_Scope6x_C': '6x',
        'Upper_CQBSS_C': '8x', 'Upper_PM2_01_C': '15x',
        'SideRail_DotSight_RMR_C': '侧瞄',
        'Muzzle_Compensator_Large_C': '补偿', 'Muzzle_Compensator_Medium_C': '补偿',
        'Muzzle_Compensator_SniperRifle_C': '补偿',
        'Muzzle_Suppressor_Large_C': '消音', 'Muzzle_Suppressor_Medium_C': '消音',
        'Muzzle_Suppressor_Small_C': '消音', 'Muzzle_Suppressor_SniperRifle_C': '消音',
        'Muzzle_FlashHider_Large_C': '消焰', 'Muzzle_FlashHider_Medium_C': '消焰',
        'Muzzle_FlashHider_SniperRifle_C': '消焰',
        # Both names: 41.1 replaced the Angled Foregrip with the Tilted Grip,
        # and the old asset still appears in older captures. (It was also
        # pinned by a class list whose ORDER could not be edited; that list
        # died with the fire-mode CNN on 2026-08-08, so only the captures
        # keep the old name alive now.)
        'Lower_Foregrip_C': '垂直', 'Lower_TiltedGrip_C': '斜向',
        'Lower_AngledForeGrip_C': '三角(已移除)',
        'Lower_HalfGrip_C': '半截', 'Lower_ThumbGrip_C': '拇指',
        'Lower_LightweightForeGrip_C': '轻型', 'Lower_LaserPointer_C': '激光',
        'Lower_Foregrip_Crossbow': '弩垂', 'Lower_QuickDraw_Large_Crossbow_C': '弩快',
        'Vector_VerGrip': '垂直',
        # 枪托。名字取自 attachment_catalog 的 `asset` 字段, 那是检测器实际
        # 读回来的东西 —— 这五条以前一条都没有, 而 'Lower_Sniper_CheekPad_
        # Vss_setting': '腮托' 曾经挂在这里, 是个 Lower_(握把槽) 名字, 对不
        # 上任何一个真实读数。它看起来像覆盖了腮托, 于是没人去补剩下四个。
        'Stock_SniperRifle_CheekPad_C': '腮托',
        'Stock_Heavy_C': '加重', 'Stock_AR_Composite_C': '战术',
        'Stock_SniperRifle_BulletLoops_C': '弹袋', 'Stock_UZI_C': 'UZI托',
    }

    def _short(self, name):
        return self._ATTACH_CN.get(name) or ('-' if not name else name[:4])

    def _fmt(self, w, is_active):
        mark = '*' if is_active else ' '
        if not w.name:
            return f'  {mark} (empty)'
        left = f'{w.name} | {w.fire_mode or "?"}'
        # Read straight off the Weapon: detector/weapon.py's __init__ assigns
        # scope / muzzle / grip unconditionally, so the three-arg getattr could
        # never fire. It was not free either — if that class ever moves its
        # attachments into a dict, the fallback would print three blanks and
        # this status line would quietly stop reporting what the gun wears.
        # Reading directly turns that into an AttributeError on the first tick.
        scope = self._short(w.scope)
        muzzle = self._short(w.muzzle)
        grip = self._short(w.grip)
        # ⚠ 枪托这一列 2026-08-07 才有, 而 set_seq() 一直在按它算压枪。VSS 装
        # 上腮托 sum|dy| 从 1696.4 掉到 1292.7 (实测因子 0.762, kit 档), 而状态
        # 行只有 scope|muzzle|grip 三列, 打出来是一排 `-`。三个槽全空的枪看着
        # 像裸枪, 补偿却已经按装配后的曲线在发 —— 差 24% 而屏幕上没有出处。
        butt = self._short(w.butt)
        right = f'{scope:>4s} | {muzzle:>5s} | {grip:>5s} | {butt:>5s}'
        return (f'  {mark} {left:<16s}  {right} | {self.posture:<8s} | '
                f'{self._curve(w)}')

    def _curve(self, w):
        """这把枪此刻装着的曲线，一列。

        ⚠ 它 2026-08-10 才有，而这张表 2026-08-10 起是终端上**唯一**的东西。
        在那之前「压没压枪」只由 `[armed]` 那行说，配件由这张表说 —— 两行分
        属两个通道，而 `[armed]` 现在整个进了日志文件。**一张只印配件、不印
        有没有曲线的表，和一把没在压枪的枪，在屏幕上长得一模一样**，那正是
        这个仓库反复付账的那个形状。

        为什么**不**印原因（没测过？配件没读过？火力模式不对？）：那是
        `detector/weapon.py` 那几段长文，它们仍然照写，只是写进日志文件。
        **状态归屏幕，出处归文件。**
        """
        if not w.dy_s:
            return '无曲线'
        return f'{len(w.dy_s)}发 {sum(w.dy_s):.0f}c'
