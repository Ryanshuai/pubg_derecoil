import os
import numpy as np
from calibrate_distance.write_dict import write_to_file_abs

ballistic = {
    'm762': (
        (1, 0),
        (2, 44),
        (3, 24),
        (5, 28),
        (10, 33),
        (15, 45),
        (30, 47),
        (40, 51),
    ),
    'akm': (
        (1, 0),
        (2, 42),
        (5, 25),
        (10, 32),
        (40, 40),
    ),
    'dp28': (
        (1, 0),
        (2, 30),
        (5, 20),
        (47, 30),
    ),
    'm416': (
        (1, 0),
        (2, 35),
        (4, 18),
        (10, 24),
        (15, 32),
        (30, 30),
        (40, 37),
    ),
    'qbz': (
        (1, 0),
        (2, 34),
        (5, 18),
        (10, 22),
        (15, 32),
        (20, 34),
        (40, 36),
    ),
    'scar': (
        (1, 0),
        (2, 30),
        (5, 20),
        (10, 24),
        (15, 28),
        (40, 32),
    ),
    'g36c': (
        (1, 0),
        (2, 40),
        (5, 16),
        (10, 26),
        (15, 30),
        (20, 34),
        (40, 36),
    ),
    'tommy': (
        (1, 0),
        (3, 20),
        (6, 21),
        (8, 24),
        (10, 30),
        (15, 40),
        (50, 45),
    ),
    'uzi': (
        (1, 0),
        (2, 13),
        (10, 12),
        (15, 20),
        (35, 30),
    ),
    'ump45': (
        (1, 0),
        (5, 18),
        (15, 30),
        (35, 32),
    ),
    'vector': (
        (1, 0),
        (6, 16),
        (10, 20),
        (13, 24),
        (15, 28),
        (20, 32),
        (33, 34),
    ),
}


def decode_seq(seq):
    heights = [0]
    last_i = seq[0][0]
    for i, height in seq[1:]:
        heights += [height] * (i - last_i)
        last_i = i
    return heights


for gun_name, seq in ballistic.items():
    heights = decode_seq(seq)
    write_to_file_abs(gun_name, heights)
