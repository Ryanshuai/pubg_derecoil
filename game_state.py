"""Game state — weapon names, attachments, fire mode, posture, recoil control.

Pure state model. Weapon objects auto-recalculate recoil on changes.
"""
from weapon import Weapon
from press import Press
import config

# ── Short display names for attachments ───────────────────

_SCOPE = {
    'Upper_DotSight_01_C': '1x', 'Upper_Holosight_C': '1x holo',
    'Upper_Aimpoint_C': '2x', 'Upper_Scope3x_C': '3x',
    'Upper_ACOG_01_C': '4x', 'Upper_Scope6x_C': '6x',
    'Upper_CQBSS_C': '8x', 'Upper_PM2_01_C': '15x',
    'SideRail_DotSight_RMR_C': 'canted',
}

_ATTACH = {
    'Muzzle_Compensator_Large_C': 'comp', 'Muzzle_Compensator_Medium_C': 'comp',
    'Muzzle_Compensator_SniperRifle_C': 'comp',
    'Muzzle_Suppressor_Large_C': 'supp', 'Muzzle_Suppressor_Medium_C': 'supp',
    'Muzzle_Suppressor_Small_C': 'supp', 'Muzzle_Suppressor_SniperRifle_C': 'supp',
    'Muzzle_FlashHider_Large_C': 'flash', 'Muzzle_FlashHider_Medium_C': 'flash',
    'Muzzle_FlashHider_SniperRifle_C': 'flash',
    'Muzzle_Choke_C': 'choke', 'Muzzle_Duckbill_C': 'duck',
    'Lower_Foregrip_C': 'vert', 'Lower_AngledForeGrip_C': 'angled',
    'Lower_HalfGrip_C': 'half', 'Lower_ThumbGrip_C': 'thumb',
    'Lower_LightweightForeGrip_C': 'light', 'Lower_LaserPointer_C': 'laser',
    'Lower_Foregrip_Crossbow': 'vert', 'Lower_QuickDraw_Large_Crossbow_C': 'qd',
    'Lower_Sniper_CheekPad_Vss_setting': 'cheek', 'Vector_VerGrip': 'vert',
    'Stock_AR_Composite_C': 'tac', 'Stock_SniperRifle_CheekPad_C': 'cheek',
    'Stock_SniperRifle_BulletLoops_C': 'loops', 'Stock_Shotgun_BulletLoops_C': 'loops',
    'Stock_UZI_C': 'stock',
}


def _short(name):
    if name in _SCOPE:
        return _SCOPE[name]
    if name in _ATTACH:
        return _ATTACH[name]
    if 'ExtendedQuickDraw' in name: return 'ext+qd'
    if 'Extended_DrumMagazine' in name: return 'drum'
    if 'Extended' in name: return 'ext'
    if 'QuickDraw' in name: return 'qd'
    return name

_SLOT_TO_ATTR = {'scope': 'scope', 'muzzle': 'muzzle', 'grip': 'grip', 'stock': 'butt'}


class GameState:
    def __init__(self):
        self.weapon_1 = Weapon()
        self.weapon_2 = Weapon()
        self.active = self.weapon_1
        self.fire_mode = ''
        self.posture = 'standing'
        self.stop_recoil = False
        self.gt_valid = False
        self.tab_open = False
        self._press = None
        self._apply_handlers = {
            'active':      lambda v: self.set_active(1 if v == 'weapon_1' else 2),
            'stop_recoil': lambda v: setattr(self, 'stop_recoil', v),
            'gt_valid':    lambda v: setattr(self, 'gt_valid', v),
            'counts':      self._adjust_counts,
            'start_press': lambda _: self.start_press(),
            'stop_press':  lambda _: self.stop_press(),
        }

    # ── Generic dispatch (driven by KEY_STATE_TABLE) ─────────

    def apply(self, field, value):
        handler = self._apply_handlers.get(field)
        if handler:
            handler(value)

    def _adjust_counts(self, delta):
        self.active.adjust_scale(delta)
        name = self.active.name or '(empty)'
        print(f"[scale] {name} = {self.active.scale:.3f}", flush=True)

    # ── Recoil control ───────────────────────────────────────

    def start_press(self):
        w = self.active
        if self.stop_recoil or len(w.dy_s) == 0:
            return
        self._press = Press(w.dx_s, w.dy_s, w.t_s)
        self._press.start()

    def stop_press(self):
        if self._press:
            self._press.stop()
            self._press = None

    # ── Weapon state ─────────────────────────────────────────

    def set_weapon(self, slot, name):
        w = self.weapon_1 if slot == 1 else self.weapon_2
        w.set('name', name)
        w.set_seq()
        self._print_status()

    def set_active(self, slot):
        self.active = self.weapon_1 if slot == 1 else self.weapon_2
        self._print_status()

    def set_fire_mode(self, mode):
        self.fire_mode = mode
        self.active.set('fire_mode', mode)
        self.active.set_seq()
        self._print_status()

    def set_posture(self, posture):
        self.posture = posture
        for w in (self.weapon_1, self.weapon_2):
            w.set('posture', posture)
            w.set_seq()

    def set_attachments(self, slot, attachments):
        """attachments: dict {scope, muzzle, grip, magazine, stock} → class name or ''."""
        w = self.weapon_1 if slot == 1 else self.weapon_2
        for slot_name, val in attachments.items():
            attr = _SLOT_TO_ATTR.get(slot_name)
            if attr:
                w.set(attr, val)
        w.set_seq()

    def reload_seq(self):
        for w in (self.weapon_1, self.weapon_2):
            w.bullet_calculator.counts_per_unit = config.COUNTS_PER_RECOIL_UNIT
            w.set_seq()

    # ── Display ──────────────────────────────────────────────

    def _format_weapon(self, w, is_active):
        mark = '*' if is_active else ' '
        if not w.name:
            return f'  {mark} (empty)'
        parts = [w.name, w.fire_mode or '?']
        scope = _SCOPE.get(getattr(w, 'scope', ''), '')
        if scope:
            parts.append(scope)
        for attr, label in [('muzzle', 'muz'), ('grip', 'grip'), ('butt', 'stock')]:
            val = getattr(w, attr, '')
            if val:
                parts.append(f'{label}={_short(val)}')
        if self.posture != 'standing':
            parts.append(self.posture)
        return f'  {mark} ' + ' | '.join(parts)

    def _print_status(self):
        sep = '--------------------------------------'
        l1 = self._format_weapon(self.weapon_1, self.weapon_1 is self.active)
        l2 = self._format_weapon(self.weapon_2, self.weapon_2 is self.active)
        print(f'{sep}\n{l1}\n{l2}', flush=True)
