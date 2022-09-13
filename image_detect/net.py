import torch
import torch.nn as nn


class Flatten(torch.nn.Module):
    def forward(self, x):
        batch_size = x.shape[0]
        return x.view(batch_size, -1)


def make_layers(input_h, input_w, output_size):
    cfg = [("C", 8), ("C", 8), ('M', 0),
           ("C", 16), ("C", 16), ('M', 0),
           # ("C", 16), ("C", 16), ('M', 0),
           ("C", 32), ("C", 32), ('M', 0),
           ("C", 64), ("C", 64), ('M', 0),
           ("C", 128), ("C", 128), ('M', 0),
           ("FL", 0),
           ("F", 0), ("F", 0), ("F", 0),
           ("E", output_size)]

    layers = []
    ch = 3
    maxpool_counter = 0
    for layer, para in cfg:
        if layer == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
            maxpool_counter += 1
        if layer == "C":
            conv2d = nn.Conv2d(ch, para, kernel_size=3, padding=1)
            layers += [conv2d, nn.BatchNorm2d(para), nn.ReLU(inplace=True)]
            ch = para

        if layer == "FL":
            layers += [Flatten()]

            factor = 2 ** maxpool_counter
            assert input_h % factor == 0 and input_w % factor == 0
            fc_in_feature = (input_h // factor) * (input_w // factor) * ch

        if layer == "F":
            layers += [nn.Linear(fc_in_feature, fc_in_feature // 2), nn.ReLU(inplace=True), nn.Dropout(0.1)]
            fc_in_feature = fc_in_feature // 2

        if layer == "E":
            layers += [nn.Linear(fc_in_feature, para)]

    return nn.Sequential(*layers)


class VGG(nn.Module):
    def __init__(self, in_hw, output_size):
        super(VGG, self).__init__()
        input_h, input_w = in_hw
        self.features = make_layers(input_h, input_w, output_size)

    def forward(self, x):
        x = self.features(x)
        return x

