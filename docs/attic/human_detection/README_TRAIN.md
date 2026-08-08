# PUBG Human Detection - RT-DETR-l Training

## Task
Train RT-DETR-l model for PUBG in-game human body and head detection.

## Dataset

- **Location**: `merged/`
- **Format**: YOLOv8 (images + YOLO txt labels)
- **Classes**: 2 — `0: body`, `1: head`
- **Split**:
  - Train: 2195 images (`merged/train/images/`, `merged/train/labels/`)
  - Valid: 564 images (`merged/valid/images/`, `merged/valid/labels/`)
- **Total**: 2759 images
- **Source**: Merged from 3 Roboflow PUBG datasets (pubg_ai, pubg_player_detector, pubg_yolo)
- **Config**: `merged/data.yaml`

```yaml
names:
- body
- head
nc: 2
path: <absolute_path_to>/training_data/human_detection/merged
train: train/images
val: valid/images
```

> **Note**: `data.yaml` 里的 `path` 需要改成实际的绝对路径。

## Training Code

```python
from ultralytics import YOLO

model = YOLO('rtdetr-l.pt')
results = model.train(
    data='merged/data.yaml',
    epochs=50,
    imgsz=640,
    batch=16,           # 8×A100 可以开更大, 如 batch=128
    device=0,           # 多卡: device=[0,1,2,3,4,5,6,7]
    project='runs',
    name='pubg_detect',
    exist_ok=True,

    # Augmentation
    hsv_h=0.02,
    hsv_s=0.8,
    hsv_v=0.5,
    degrees=10,
    translate=0.15,
    scale=0.7,
    fliplr=0.5,
    flipud=0,
    mosaic=1.0,
    mixup=0.1,
    copy_paste=0.1,
    erasing=0.3,
)
print('Training done!')
```

## Dependencies

```
pip install ultralytics
```

## Multi-GPU

8×A100 时修改参数：
```python
device=[0,1,2,3,4,5,6,7]
batch=128   # 或更大, 每卡16
```

## Output

训练结果保存在 `runs/pubg_detect/`:
- `weights/best.pt` — 最优模型
- `weights/last.pt` — 最后一轮
- `results.csv` — 训练指标
- `confusion_matrix.png` — 混淆矩阵

训练完成后把 `best.pt` 拷回即可使用。
