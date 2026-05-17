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

    @property
    def active_slot(self):
        return 1 if self.active is self.weapon_1 else 2

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
        """Apply effective weapon names to Weapon objects. Call after gt/pred change."""
        w1, w2 = self.weapon_name
        for slot, name, w in [(1, w1, self.weapon_1), (2, w2, self.weapon_2)]:
            if name and name != w.name:
                w.set('name', name)
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

    def clear_attachments(self):
        """Clear attachments on both weapons (keep weapon name). Used on pickup (F)."""
        self.attachments = {}
        for w in (self.weapon_1, self.weapon_2):
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

    def toggle_tab_open(self):
        self.tab_open = not self.tab_open

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
        if new != getattr(self, '_last_status', ''):
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
        'Lower_Foregrip_C': '垂直', 'Lower_AngledForeGrip_C': '三角',
        'Lower_HalfGrip_C': '半截', 'Lower_ThumbGrip_C': '拇指',
        'Lower_LightweightForeGrip_C': '轻型', 'Lower_LaserPointer_C': '激光',
        'Lower_Foregrip_Crossbow': '弩垂', 'Lower_QuickDraw_Large_Crossbow_C': '弩快',
        'Lower_Sniper_CheekPad_Vss_setting': '腮托', 'Vector_VerGrip': '垂直',
    }

    def _short(self, name):
        return self._ATTACH_CN.get(name) or ('-' if not name else name[:4])

    def _fmt(self, w, is_active):
        mark = '*' if is_active else ' '
        if not w.name:
            return f'  {mark} (empty)'
        left = f'{w.name} | {w.fire_mode or "?"}'
        scope = self._short(getattr(w, 'scope', ''))
        muzzle = self._short(getattr(w, 'muzzle', ''))
        grip = self._short(getattr(w, 'grip', ''))
        right = f'{scope:>4s} | {muzzle:>5s} | {grip:>5s}'
        return f'  {mark} {left:<16s}  {right} | {self.posture}'
