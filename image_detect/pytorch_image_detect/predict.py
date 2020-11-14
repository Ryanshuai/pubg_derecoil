from torchvision import transforms
import torch

from PIL import Image
import cv2
import os
import numpy as np

try:
    from .net import VGG
except ImportError:
    from image_detect.pytorch_image_detect.net import VGG

model = VGG(26)
try:
    model.load_state_dict(torch.load('loss_0.001207__acc_5.000000.pth.tar'))
except:
    model.load_state_dict(torch.load('image_detect/pytorch_image_detect/loss_0.001207__acc_5.000000.pth.tar'))

model.eval()

i_name = ["ang", "null", "burst2", "burst3", "com_ar", "com_sm", "fla_ar", "fla_sm", "full", "hal", "in_tab", "las",
          "lig", "single", "sto", "sup_ar", "thu", "ver", "x1h", "x1r", "x2", "x3", "x4", "x6", "x8", "x15", ]

preprocess = transforms.Compose([
    transforms.Resize((64, 64)),
    transforms.ToTensor(),
])


def image2name(im):
    if not isinstance(im, Image.Image):
        im = Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
    im = preprocess(im)
    input_batch = im.unsqueeze(0)  # create a mini-batch as expected by the model

    with torch.no_grad():
        output = model(input_batch)
    idx = int(np.argmax(output[0]))
    # print(idx)
    return i_name[idx]


if __name__ == '__main__':

    test_dir = "test"
    for image_name in os.listdir(test_dir):
        image_path = os.path.join(test_dir, image_name)
        im = Image.open(image_path).convert('RGB')

        name = image2name(im)

        print(image_name + "----->" + name)
