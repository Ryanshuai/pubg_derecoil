# 自瞄（human detection）—— 2026-08-08 停掉，代码留档

操作员的原话：「这个将来再做吧，这个也不是短期能做出来，直接全扔掉吧。」

## 扔掉了什么

```
human_detection/                  394M    best.pt / best_v3.pt (各 198M) + test/
training_data/human_detection/    850M    merged/ pubg_ai/ pubg_imo8q/ pubg_yolo/
                                          rtdetr-l.pt · yolov8n.pt · yolo26n.pt
                                          runs/detect/**/best.pt
```

**权重和数据集不可再生**，这里留下的三个文件是可再生的那一半（推理、训练、
数据集配置），一共 20K。留它们的理由不是"说不定还要用"，是**重来一次的时候，
难的那部分是这些文件里的判断**，不是重新下一个 yolov8n。

## 为什么它可以整棵删而不碰任何别的东西

```
grep -rn "human_detection" --include=*.py --include=*.md --include=*.toml .
  → pixi.toml:44 的一行注释，和它自己的 README
```

自瞄从来没接进主回路：`control/aim.py` 是**视角驱动**（`turn` / `recenter`），
一个模型都不加载，跟这里同名只是巧合。

## 顺带死掉的：`ultralytics`

删之前全仓只有两个文件 `from ultralytics import YOLO`，**两个都在这两棵树里**。
所以 `pixi.toml` 的 `ultralytics = "*"` 现在没有任何 import 了。

⚠ **它故意还留在 pixi.toml 里**，因为拿掉它是两件事而不是一件：

1. 会触发一次环境重解，而这个仓库同时有别的 agent 在跑。
2. `detector/` 里有**六处**防御性代码是写给它的——`ammo_detector`、
   `lobby_detector`、`lobby_nav`、`tab_layout`、`weapon_template_detector`
   都写着「anything importing ultralytics replaces cv2.imread」，各自带一个
   通道数或 flag 的兜底。那些兜底现在守着一个不会发生的事，但它们无害，
   而一次性拆六处是独立的一轮。

要收这笔的时候，判据是可查的：`grep -rn "^from ultralytics\|^import ultralytics"`
为零 → 从 pixi.toml 拿掉 → 那六处兜底连同它们的注释一起清。
