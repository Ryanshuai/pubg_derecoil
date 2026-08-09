"""GameState — pure game state + mutation methods.

No key dispatch, no detection scheduling, no hardware communication.
Just state and methods to change it.
"""
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
        self.highlight_gt = 0           # from key 1/2
        self.highlight_gt_ts = 0.0      # when highlight_gt was last set
        self.highlight_pred = 0         # from CV highlight algorithm
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

    # ════════════════════════════════════════════════════════════
    # Active weapon — two sources
    # ════════════════════════════════════════════════════════════

    def set_active_by_key(self, slot):
        """Key 1/2 pressed — GT, authoritative."""
        import time
        self.active = self.weapon_1 if slot == 1 else self.weapon_2
        self.highlight_gt = slot
        self.highlight_gt_ts = time.perf_counter()

    def set_active_by_detect(self, slot):
        """Algorithm prediction — only applies when no GT."""
        if self.highlight_gt:
            return
        self.active = self.weapon_1 if slot == 1 else self.weapon_2
        self.highlight_pred = slot

    # ════════════════════════════════════════════════════════════
    # Weapon name — two sources
    # ════════════════════════════════════════════════════════════

    @property
    def weapon_name(self):
        """Effective weapon names: GT > pred > existing."""
        w1 = self.weapon_gt[0] or self.weapon_pred[0] or self.weapon_1.name
        w2 = self.weapon_gt[1] or self.weapon_pred[1] or self.weapon_2.name
        return (w1, w2)

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
            if name and name != w.name:
                was = w.name
                w.set('name', name)
                self.clear_attachments(slot)
                print(f'[state] gun {slot}: {was or "(empty)"} -> {name}, '
                      f'kit cleared (a different weapon wears different '
                      f'parts; Tab will read the new one)', flush=True)
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
        return f'  {mark} {left:<16s}  {right} | {self.posture}'
