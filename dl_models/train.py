"""
Unified trainer for all HUD detection tasks.

Usage:
    python dl_models/train.py --task weapon
    python dl_models/train.py --task tab_detect
"""
import argparse
import os
import sys

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision.models import mobilenet_v3_small

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dl_models.dataset import BackgroundProvider, SyntheticHUDDataset
from dl_models.icon_layout import WeaponIconLayout, TabDetectLayout, AttachmentIconLayout

# ── Task configs ──
BG_DIR = os.path.join(os.path.dirname(__file__), '..', 'training_data', 'backgrounds')

TASKS = {
    'weapon': {
        'layout_cls': WeaponIconLayout,
        'save_name': 'gun_name.pth.tar',
        'train_samples': 16000,
        'val_samples': 2000,
        'epochs': 20,
        'lr': 3e-4,
        'batch_size': 64,
        'hidden_dim': 1024,
    },
    'tab_detect': {
        'layout_cls': TabDetectLayout,
        'save_name': 'tab_detect.pth.tar',
        'train_samples': 8000,
        'val_samples': 1000,
        'epochs': 15,
        'lr': 3e-4,
        'batch_size': 64,
        'hidden_dim': 128,
    },
    'attachment': {
        'layout_cls': AttachmentIconLayout,
        'save_name': 'weapon_attachment.pth.tar',
        'train_samples': 32000,
        'val_samples': 4000,
        'epochs': 25,
        'lr': 2e-4,
        'batch_size': 64,
        'hidden_dim': 512,
    },
}


# ── Model ──

class MultiHeadMobileNet(nn.Module):
    """MobileNetV3-Small with configurable input channels, hidden dim, and heads."""

    def __init__(self, head_sizes, in_channels=4, hidden_dim=1024):
        """
        head_sizes: dict {name: num_classes}
        in_channels: 3 (BGR) or 4 (BGR+dewhite)
        hidden_dim: shared classifier hidden layer size
        """
        super().__init__()
        backbone = mobilenet_v3_small(weights=None)
        if in_channels != 3:
            old_conv = backbone.features[0][0]
            backbone.features[0][0] = nn.Conv2d(
                in_channels, old_conv.out_channels,
                kernel_size=old_conv.kernel_size, stride=old_conv.stride,
                padding=old_conv.padding, bias=old_conv.bias is not None,
            )
        self.features = backbone.features
        self.avgpool = backbone.avgpool
        self.shared = nn.Sequential(
            nn.Linear(576, hidden_dim),
            nn.Hardswish(inplace=True),
            nn.Dropout(p=0.2),
        )
        self.heads = nn.ModuleDict({
            name: nn.Linear(hidden_dim, n_cls) for name, n_cls in head_sizes.items()
        })

    def forward(self, x):
        x = self.features(x)
        x = self.avgpool(x)
        x = x.flatten(1)
        x = self.shared(x)
        return {name: head(x) for name, head in self.heads.items()}


# ── Training loop ──

def train(task_name):
    cfg = TASKS[task_name]
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    save_path = os.path.join(os.path.dirname(__file__), '..', 'detector', cfg['save_name'])

    print(f'Task: {task_name}, Device: {device}')

    # Data
    bg = BackgroundProvider(BG_DIR)
    layout = cfg['layout_cls']()
    head_sizes = layout.label_names
    print(f'Backgrounds: {len(bg.images)}')
    print(f'Crop: {layout.crop_hw}, Input: {layout.model_input_hw}, '
          f'Channels: {layout.in_channels}')
    print(f'Heads: {head_sizes}')

    train_ds = SyntheticHUDDataset(bg, layout, cfg['train_samples'], augment=True)
    val_ds = SyntheticHUDDataset(bg, layout, cfg['val_samples'], augment=False)
    train_loader = DataLoader(train_ds, cfg['batch_size'], shuffle=True,
                              num_workers=0, pin_memory=True)
    val_loader = DataLoader(val_ds, cfg['batch_size'], shuffle=False,
                            num_workers=0, pin_memory=True)

    # Model
    model = MultiHeadMobileNet(
        head_sizes,
        in_channels=layout.in_channels,
        hidden_dim=cfg['hidden_dim'],
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=cfg['lr'])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, cfg['epochs'])
    criterion = nn.CrossEntropyLoss()

    params = sum(p.numel() for p in model.parameters())
    print(f'Model params: {params/1e3:.1f}K')

    best_acc = 0.0
    primary_head = list(head_sizes.keys())[0]

    for epoch in range(1, cfg['epochs'] + 1):
        # Train
        model.train()
        total_loss = 0.0
        correct = {k: 0 for k in head_sizes}
        total = 0
        for imgs, labels in train_loader:
            imgs = imgs.to(device)
            labels = {k: v.to(device) for k, v in labels.items()}
            outs = model(imgs)

            loss = sum(criterion(outs[k], labels[k]) for k in head_sizes)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * imgs.size(0)
            for k in head_sizes:
                correct[k] += (outs[k].argmax(1) == labels[k]).sum().item()
            total += imgs.size(0)
        scheduler.step()

        train_acc = {k: correct[k] / total for k in head_sizes}

        # Val
        model.eval()
        correct = {k: 0 for k in head_sizes}
        total = 0
        with torch.no_grad():
            for imgs, labels in val_loader:
                imgs = imgs.to(device)
                labels = {k: v.to(device) for k, v in labels.items()}
                outs = model(imgs)
                for k in head_sizes:
                    correct[k] += (outs[k].argmax(1) == labels[k]).sum().item()
                total += imgs.size(0)
        val_acc = {k: correct[k] / total for k in head_sizes}

        # Print
        acc_str = '  '.join(
            f'{k}: train={train_acc[k]:.3f} val={val_acc[k]:.3f}'
            for k in head_sizes
        )
        print(f'Epoch {epoch:2d}/{cfg["epochs"]}  loss={total_loss/total:.4f}  '
              f'{acc_str}  lr={scheduler.get_last_lr()[0]:.1e}')

        if val_acc[primary_head] > best_acc:
            best_acc = val_acc[primary_head]
            torch.save(model.state_dict(), save_path)
            print(f'  -> saved ({primary_head}_val={best_acc:.3f})')

    print(f'\nDone. Best {primary_head}_val_acc={best_acc:.3f}, saved to {save_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True, choices=list(TASKS.keys()))
    args = parser.parse_args()
    train(args.task)
