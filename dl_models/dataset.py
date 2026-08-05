"""
Synthetic HUD dataset — pure generic container.

Takes any layout object (from icon_layout.py) through uniform interface:
  layout.crop_hw         → canvas size
  layout.model_input_hw  → resize target
  layout.in_channels     → tensor channels
  layout.apply(canvas)   → composite + labels
  layout.preprocess(img) → BGR → model input

dataset.py knows nothing about weapons, tabs, or any specific task.
"""
import os
import re
import random

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import SCREEN_H


# ═══════════════════════════════════════════════════════════════
#  BackgroundProvider
# ═══════════════════════════════════════════════════════════════

class BackgroundProvider:
    """Loads background images, scales to SCREEN_H, provides random crops."""

    def __init__(self, image_dir, screen_h=SCREEN_H):
        self.images = []
        for fname in sorted(os.listdir(image_dir)):
            if os.path.splitext(fname)[1].lower() not in ('.png', '.jpg', '.jpeg', '.bmp', '.webp'):
                continue
            im = cv2.imread(os.path.join(image_dir, fname))
            if im is None:
                continue
            h, w = im.shape[:2]
            scale = screen_h / h
            self.images.append(
                cv2.resize(im, (int(w * scale), screen_h), interpolation=cv2.INTER_AREA)
            )
        if not self.images:
            raise ValueError(f"No background images found in {image_dir}")

    def random_crop(self, crop_h, crop_w):
        bg = random.choice(self.images)
        h, w = bg.shape[:2]
        x = random.randint(0, max(w - crop_w, 0))
        y = random.randint(0, max(h - crop_h, 0))
        crop = bg[y:y + crop_h, x:x + crop_w].copy()
        if crop.shape[0] < crop_h or crop.shape[1] < crop_w:
            padded = np.zeros((crop_h, crop_w, 3), dtype=np.uint8)
            padded[:crop.shape[0], :crop.shape[1]] = crop
            crop = padded
        return crop


# ═══════════════════════════════════════════════════════════════
#  SyntheticHUDDataset
# ═══════════════════════════════════════════════════════════════

class SyntheticHUDDataset(Dataset):
    """
    Generic: background + layout → (tensor, labels).

    不知道具体任务, 只调用 layout 的统一接口.
    """

    def __init__(self, bg_provider, layout, samples_per_epoch=16000, augment=True):
        self.bg = bg_provider
        self.layout = layout
        self.samples_per_epoch = samples_per_epoch
        self.augment = augment
        self.crop_h, self.crop_w = layout.crop_hw
        self.input_h, self.input_w = layout.model_input_hw

    def __len__(self):
        return self.samples_per_epoch

    def _augment(self, img):
        img = img.astype(np.float32)
        img += random.uniform(-15, 15)
        img *= random.uniform(0.85, 1.15)
        for c in range(3):
            img[:, :, c] += random.uniform(-8, 8)
        if random.random() < 0.5:
            img += np.random.normal(0, random.uniform(1, 4), img.shape)
        r = random.random()
        if r < 0.25:
            img = cv2.GaussianBlur(img, (3, 3), random.uniform(0.5, 1.0))
        elif r < 0.5:
            blur = cv2.GaussianBlur(img, (3, 3), 1.0)
            img = img + random.uniform(0.3, 0.8) * (img - blur)
        return np.clip(img, 0, 255).astype(np.uint8)

    def __getitem__(self, idx):
        canvas = self.bg.random_crop(self.crop_h, self.crop_w)
        labels = self.layout.apply(canvas)

        if self.augment:
            canvas = self._augment(canvas)

        canvas = cv2.resize(canvas, (self.input_w, self.input_h))
        processed = self.layout.preprocess(canvas)

        tensor = torch.from_numpy(
            processed.transpose(2, 0, 1).astype(np.float32) / 255.0
        )
        return tensor, labels


# ═══════════════════════════════════════════════════════════════
#  RealClassifyDataset (generic: posture, fire_mode, etc.)
# ═══════════════════════════════════════════════════════════════

class RealClassifyDataset(Dataset):
    """
    Real in-game screenshots. Filename: {class}_{hash}.png
    Files in a flat directory (no subdirectories).
    """

    def __init__(self, data_dir, layout, head_name, class_list,
                 augment=True, oversample=1):
        self.layout = layout
        self.augment = augment
        self.head_name = head_name
        self.input_h, self.input_w = layout.model_input_hw

        cls_set = set(class_list)
        self.samples = []  # (path, label)
        for cls_name in os.listdir(data_dir):
            cls_dir = os.path.join(data_dir, cls_name)
            if not os.path.isdir(cls_dir):
                continue
            if cls_name == 'bg':
                label = 0
            elif cls_name in cls_set:
                label = class_list.index(cls_name) + 1
            else:
                print(f'RealClassifyDataset({head_name}): skipping "{cls_name}"')
                continue
            for fname in os.listdir(cls_dir):
                if not fname.endswith('.png'):
                    continue
                self.samples.append((os.path.join(cls_dir, fname), label))

        self.samples *= oversample
        print(f'RealClassifyDataset({head_name}): {len(self.samples)} samples '
              f'({len(self.samples) // oversample} unique, {oversample}x)')

    def __len__(self):
        return len(self.samples)

    def _augment(self, img):
        img = img.astype(np.float32)
        img += random.uniform(-15, 15)
        img *= random.uniform(0.85, 1.15)
        for c in range(3):
            img[:, :, c] += random.uniform(-8, 8)
        if random.random() < 0.5:
            img += np.random.normal(0, random.uniform(1, 4), img.shape)
        r = random.random()
        if r < 0.25:
            img = cv2.GaussianBlur(img, (3, 3), random.uniform(0.5, 1.0))
        elif r < 0.5:
            blur = cv2.GaussianBlur(img, (3, 3), 1.0)
            img = img + random.uniform(0.3, 0.8) * (img - blur)
        return np.clip(img, 0, 255).astype(np.uint8)

    def __getitem__(self, idx):
        path, label = self.samples[idx]
        img = cv2.imread(path)
        img = cv2.resize(img, (self.input_w, self.input_h))
        if self.augment:
            img = self._augment(img)
        processed = self.layout.preprocess(img)
        tensor = torch.from_numpy(
            processed.transpose(2, 0, 1).astype(np.float32) / 255.0
        )
        return tensor, {self.head_name: label}
