import threading
import time
from pynput import keyboard, mouse

from press import Press
from weapon import Weapon
from image_detect.detector import Detector
from calibrate_distance.bullet_detect import Updater


class Robot:
    def __init__(self, is_calibrating=False):
        self.weapon_1 = Weapon(is_calibrating)
        self.weapon_2 = Weapon(is_calibrating)
        self.weapon = self.weapon_1
        self.fire_mode = ""

        self.is_calibrating = is_calibrating

        self.is_in_tab = False
        self.stop_press = False
        self.tab_func_time = time.time()

        self.fire_mode_detector = Detector('fire_mode')
        self.in_tab_detector = Detector('in_tab')
        # self.posture_detect = Detector('posture')
        # self.in_scope_detect = Detector('in_scope')

        self.gun_detector = dict()
        for gun_pos_name in ['gun1_name', 'gun1_scope', 'gun1_muzzle', 'gun1_grip', 'gun1_butt',
                             'gun2_name', 'gun2_scope', 'gun2_muzzle', 'gun2_grip', 'gun2_butt']:
            self.gun_detector[gun_pos_name] = Detector(gun_pos_name)

        self.key_listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.key_listener.start()
        self.mouse_listener.start()

        if is_calibrating:
            self.updater = Updater()
        print("init done")

    def on_release(self, key):
        if key in [keyboard.Key.f12, keyboard.Key.esc]:
            self.stop_press = True

        if key == keyboard.Key.tab:
            self.stop_press = False

        if key == keyboard.Key.tab and time.time() > self.tab_func_time:# + 1:
            self.in_tab_detect()

        if hasattr(key, 'char'):
            key = key.char

        if key == "1":
            self.weapon = self.weapon_1

        if key == "2":
            self.weapon = self.weapon_2

        if key in ["1", "2", "b"]:
            self.stop_press = False
            time.sleep(0.1)
            self.fire_mode = self.fire_mode_detector()

    def on_press(self, key):
        if key == keyboard.Key.enter and self.is_calibrating:
            print("start calibrating")
            self.updater.update(self.weapon.name)

        if key == keyboard.Key.backspace and self.is_calibrating:
            print("end calibrating")
            self.updater.determine()

        if hasattr(key, 'char'):
            key = key.char
        if key == 'g' or key == '5':
            self.stop_press = True

    def on_click(self, x, y, button, pressed):
        if button == mouse.Button.left and pressed and not self.is_in_tab and not self.stop_press:
            self.press = Press(self.weapon.dx_s, self.weapon.dy_s, self.weapon.t_s)
            self.press.start()

        if button == mouse.Button.left and (not pressed):
            if self.is_in_tab:
                threading.Timer(0.2, self.gun_detect).start()

            if hasattr(self, 'press'):
                self.press.stop()

    def in_tab_detect(self):
        self.is_in_tab = self.in_tab_detector() == "in_tab"
        if not self.is_in_tab:
            self.tab_func_time = time.time()
            return
        self.gun_detect()

    def gun_detect(self):
        for gun_pos, detector in self.gun_detector.items():
            pos_detect_res = detector()
            gun, pos = gun_pos.split('_')
            if gun == "gun1":
                self.weapon_1.set(pos, pos_detect_res)
            if gun == "gun2":
                self.weapon_2.set(pos, pos_detect_res)

        self.weapon_1.set_seq()
        self.weapon_2.set_seq()
        print(self.weapon_1)
        print(self.weapon_2)


if __name__ == '__main__':
    # is_calibrating = True
    is_calibrating = False
    robot = Robot(is_calibrating)
    robot.key_listener.run()
