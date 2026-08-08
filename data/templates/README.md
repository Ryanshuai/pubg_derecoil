# docs/training_data/ — **名字是历史的，内容不是训练数据**

2026-08-08 从仓库根搬到这里，同一天里面 566 MB 被删。剩下 19 MB，而且**这个目录
里绝大部分不是"数据"，是检测器 import 时就加载的模板**。

## 谁是活的

| | 大小 | 谁读 | 进 git 吗 |
|---|---|---|---|
| `pubg_assets/` | 974K | **8 个检测器**（ads / ammo / attachment / lobby / lobby_nav / posture / spawner / tab_layout） | **是**，69 个 |
| `ocr_white/` | 61K | `weapon_template_detector` | **是**，44 个 |
| `weapon_hud_bank.npz` | 2.8M | `weapon_hud_detector` | 否 ⚠ 见下 |
| `highlight_eval/` | 15M | `pixi run highlight`（254/254，26 把枪） | 否 |

**前两项删了检测器立刻瞎**，跟"历史数据"没有关系。判据在 `.gitignore` 里写着：
**「clean clone 跑不跑得起来」，不是「大不大」。**

## 搬家当天揭出来的两件事

`.gitignore` 那条例外注释写着「these 24 files」。`git ls-files` 说 **106**，而
规则只解禁了 `lobby/tabs/*.png` 一处（36 个）。**另外 70 个是靠惯性留在索引里的**
——gitignore 管不到已跟踪文件，它们在规则存在之前就被 add 过，然后再没离开。
没有任何东西会重新 add 它们；clean clone 一直能跑，纯粹因为没人有理由重 add。

**一次移动会强制每条 ignore 规则重新生效，所以它是唯一能看出规则实际在干什么的
事件。** 规则现在跟索引一致了，112 个。

## ⚠ `weapon_hud_bank.npz` 现在是「不可重建的被忽略文件」

它是派生的（PCA 基 + 投影），所以按惯例不进 git，注释写着「重建用
`calibration/build_weapon_hud_bank.py`」。**那句话 2026-08-08 起是假的**——它的
语料 `Manual/weapon_hud`（128 MB，5590 张）当天删了。

两者都对，合起来是错的：**一个东西不能既"因为可重建所以不跟踪"、又"没有能重建
它的输入"。** 要么把它加进 git，要么承认 weapon_hud 检测器在游戏更新后没有恢复
路径。**别让它继续停在中间那个状态而不被记下来。**

## 删掉了什么，以及为什么

```
backgrounds/          391 MB   唯一读者是 dl_models/train.py:22 (BG_DIR)
Manual/weapon_hud     121 MB   只用来重建那个 2.8M 的 npz
Manual/attachment      19 MB   全仓零读者
Manual/posture         12 MB   全仓零读者
Manual/fire_mode      8.4 MB   火力模式 CNN 的训练裁图
Manual/tab_detect       2 个   全仓零读者
```

前两项和第五项是**同一个决定的三个部分**：火力模式的 MobileNet 在 859 张难例上
只裁决 2 张，跟它的 checkpoint、语料和整条 torch 依赖一起退场
（数字在 `detector/fire_mode_detector.py` 顶部）。

⚠ **`Manual/posture` 那 12 MB 里 prone 只有 10 张，而唯一那张"失败样本"的标签
是错的**（`detector/CLAUDE.md` 逐像素复核过）。姿势识别 1714 张实测 0.993，
所以删掉的是一个**没人读、而且已知不可尽信**的集合——不是一个可惜的语料。
