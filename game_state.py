"""Game state — weapon names, attachments, fire mode, posture, recoil control.

Pure state model. Weapon objects auto-recalculate recoil on changes.
"""
from weapon import Weapon
from press import Press
import config
import display_names

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

    def on_crouch_key(self):
        """C key: standing↔crouching, prone→crouching."""
        if self.posture == 'standing':
            self.set_posture('crouching')
        elif self.posture == 'crouching':
            self.set_posture('standing')
        elif self.posture == 'prone':
            self.set_posture('crouching')

    def on_prone_key(self):
        """Z key: →prone if standing/crouching, →standing if prone."""
        if self.posture == 'prone':
            self.set_posture('standing')
        else:
            self.set_posture('prone')

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
        scope = display_names.scope_short(getattr(w, 'scope', ''))
        if scope:
            parts.append(scope)
        for attr, label in [('muzzle', 'muz'), ('grip', 'grip'), ('butt', 'stock')]:
            val = getattr(w, attr, '')
            if val:
                parts.append(f'{label}={display_names.short(val)}')
        if self.posture != 'standing':
            parts.append(self.posture)
        return f'  {mark} ' + ' | '.join(parts)

    def _print_status(self):
        sep = '--------------------------------------'
        l1 = self._format_weapon(self.weapon_1, self.weapon_1 is self.active)
        l2 = self._format_weapon(self.weapon_2, self.weapon_2 is self.active)
        print(f'{sep}\n{l1}\n{l2}', flush=True)
