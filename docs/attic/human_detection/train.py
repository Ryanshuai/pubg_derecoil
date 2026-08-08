from ultralytics import YOLO

model = YOLO('rtdetr-l.pt')
results = model.train(
    data='merged/data.yaml',
    epochs=50,
    imgsz=640,
    batch=16,
    device=0,
    project='runs',
    name='pubg_detect',
    exist_ok=True,

    # Augmentation
    hsv_h=0.02,        # hue shift
    hsv_s=0.8,         # saturation
    hsv_v=0.5,         # brightness
    degrees=10,         # slight rotation ±10°
    translate=0.15,     # translate
    scale=0.7,          # scale ±70% (different distances)
    fliplr=0.5,         # horizontal flip
    flipud=0,           # no vertical flip
    mosaic=1.0,         # mosaic augmentation
    mixup=0.1,          # mixup
    copy_paste=0.1,     # copy-paste augmentation
    erasing=0.3,        # random erasing (occlusion simulation)
)
print('Training done!')
