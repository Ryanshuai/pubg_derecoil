import os
import shutil
import threading

from PyQt5.QtCore import pyqtSignal, QObject
from pynput import keyboard, mouse

from image_detect.Detector import Detector
from press_gun.press import Press
from screen_capture import win32_cap
from state.all_states import All_States
from state.position_constant import get_pos, pos_names


class Temp_QObject(QObject):
    state_str_signal = pyqtSignal(str)


class Robot:
    def __init__(self, all_states, is_calibrating=False):
        self.all_states = all_states
        self.screen = None
        self.in_block = False
        self.in_right = False

        self.calibrate = is_calibrating

        self.fire_mode_detect = Detector('fire_mode')
        self.in_tab_detect = Detector('in_tab')
        # self.posture_detect = Detector('posture')
        # self.in_scope_detect = Detector('in_scope')

        self.gun_detector = dict()
        self.gun_detector['name'] = Detector('gun_name')
        self.gun_detector['scope'] = Detector('gun_scope')
        self.gun_detector['muzzle'] = Detector('gun_muzzle')
        self.gun_detector['grip'] = Detector('gun_grip')
        self.gun_detector['butt'] = Detector('gun_butt')
        # self.gun_detector['magazine'] = Detector('magazine')

        self.key_listener = keyboard.Listener(on_press=self.on_press)
        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.key_control = keyboard.Controller()
        self.key_listener.start()
        self.mouse_listener.start()

        self.temp_qobject = Temp_QObject()
        if is_calibrating and os.path.exists("calibrate_distance/gun_dist_screen"):
            shutil.rmtree("calibrate_distance/gun_dist_screen")

    def on_press(self, key):
        if key == keyboard.Key.tab:
            self.screen = get_screen()
            threading.Timer(0.01, self.is_in_tab).start()
        if key == keyboard.Key.f12:
            self.all_states.to_press = False

        if key == keyboard.Key.ctrl_l and self.calibrate:
            self.ctrl_save_screen()

        if hasattr(key, 'char'):
            key = key.char
        if key == 'g' or key == '5':
            self.all_states.to_press = False
        if key == 'b':
            self.all_states.to_press = True
            self.set_fire_mode()
        if key == '1' or key == '2':
            self.all_states.to_press = True
            self.all_states.set_weapon_n(int(key) - 1)
            self.set_fire_mode()

    def on_click(self, x, y, button, pressed):
        if button == mouse.Button.left and pressed and self.all_states.to_press:
        # if button == mouse.Button.left and pressed:
            n = self.all_states.weapon_n
            if self.all_states.weapon[n].is_press:
                self.press = Press(self.all_states.weapon[n].dist_seq, self.all_states.weapon[n].time_seq,
                                   self.calibrate)
                self.press.start()

        if button == mouse.Button.left and (not pressed):
            if hasattr(self, 'press'):
                self.press.stop()

        # if button in [mouse.Button.right, mouse.Button.left] and (not pressed):
        #     if self.all_states.screen_state == 'tab':
        #         threading.Timer(0.2, self.tab_func).start()

    def ctrl_save_screen(self):
        n = self.all_states.weapon_n
        gun_name = self.all_states.weapon[n].name
        save_dir = os.path.join('calibrate_distance', "gun_dist_screen", gun_name)
        if not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
        i = len(os.listdir(save_dir))
        win32_cap(os.path.join(save_dir, str(i) + ".png"))

    def is_in_tab(self):
        go_out_of_tab = 'in_tab' == self.in_tab_detect.im2name(crop_screen(self.screen, 'in_tab'))
        if go_out_of_tab:
            self.all_states.to_press = False
            self.all_states.screen_state = 'tab'
            self.tab_func()
        else:
            threading.Timer(0.2, self.set_fire_mode).start()
            self.all_states.screen_state = 'default'
            self.all_states.to_press = True

    def tab_func(self):
        for pos_name in pos_names:
            corp_im = crop_screen(self.screen, pos_name)
            pos = pos_name.split('_')[-1]
            crop_name = self.gun_detector[pos].im2name(corp_im)
            if pos_name.startswith("gun1"):
                self.all_states.weapon[0].set(pos, crop_name)
            if pos_name.startswith("gun2"):
                self.all_states.weapon[1].set(pos, crop_name)

        self.all_states.weapon[0].set_seq()
        self.all_states.weapon[1].set_seq()
        self.print_state()

    def set_fire_mode(self):
        # fire_mode_crop = get_screen('fire_mode')
        # fire_mode = self.fire_mode_detect.im2name(fire_mode_crop)
        n = self.all_states.weapon_n
        self.all_states.weapon[n].set('fire_mode', "")
        self.print_state()

    def print_state(self):
        w = self.all_states.weapon[0]
        gun1_state = str(w.name) + '-' + str(w.fire_mode) + '-' + str(w.scope) + '-' + str(w.muzzle)[:3] + '-' + str(
            w.grip) + '-' + str(w.butt)
        w = self.all_states.weapon[1]
        gun2_state = str(w.name) + '-' + str(w.fire_mode) + '-' + str(w.scope) + '-' + str(w.muzzle)[:3] + '-' + str(
            w.grip) + '-' + str(w.butt)
        if self.all_states.weapon_n == 0:
            emit_str = ' * ' + gun1_state + '\n' + gun2_state
        else:
            emit_str = gun1_state + '\n' + ' * ' + gun2_state
        self.temp_qobject.state_str_signal.emit(emit_str)


def crop_screen(screen, name):
    y, x, h, w = get_pos(name)
    return screen[y:y + h, x:x + w]


def get_screen(name=None):
    if name is None:
        return win32_cap(filename='temp_image')
    pos = get_pos(name)
    return win32_cap(filename='temp_image', rect=pos)


if __name__ == '__main__':
    all_states = All_States()
    k = Robot(all_states)
    k.key_listener.run()
