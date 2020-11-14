from win32api import GetSystemMetrics

screen_width = GetSystemMetrics(0)
screen_high = GetSystemMetrics(1)
screen_h_factor = screen_high / 1080
screen_w_factor = screen_width / 1920

show_position_y = 300 * screen_h_factor
show_position_x = 1650 * screen_w_factor
show_size_y = 50
show_size_x = 200

min_icon_area = 10 * 10 * screen_h_factor * screen_h_factor
max_icon_area = 30 * screen_h_factor * 30 * screen_h_factor
min_icon_side_len = 30 * screen_h_factor
max_icon_side_len = 50 * screen_h_factor

min_rect_side_len = 45 * screen_h_factor
max_rect_side_len = 55 * screen_h_factor

max_icon_diff = 20

white_min_rgb = {
    'name': 245,
    'in-tab': 235,
    'posture': 200,
    'fire-mode': 235,
}
min_white_rate = {
    'name': 0.04 * screen_h_factor * screen_h_factor,
    'in-tab': 0.001 * screen_h_factor * screen_h_factor,
    'posture': 0.04 * screen_h_factor * screen_h_factor,
    'fire-mode': 0.0001 * screen_h_factor * screen_h_factor
}

min_gun_name_high = 15 * screen_h_factor
max_gun_name_high = 25 * screen_h_factor
min_gun_name_width = 105 * screen_h_factor
max_gun_name_width = 120 * screen_h_factor

min_fire_mode_high = 15 * screen_h_factor
max_fire_mode_high = 25 * screen_h_factor
min_fire_mode_width = 12 * screen_h_factor
max_fire_mode_width = 17 * screen_h_factor

min_in_tab_high = 7 * screen_h_factor
max_in_tab_high = 13 * screen_h_factor
min_in_tab_width = 28 * screen_h_factor
max_in_tab_width = 34 * screen_h_factor
