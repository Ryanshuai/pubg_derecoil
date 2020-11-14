import torch.nn as nn

size_config = {
    64: [[6, 'M', 12, 'M', 12, 'M'], [768, 256, 128]],
    32: [[6, 'M', 12, 'M'], [768, 256, 128]],
}


def make_layers(cfg, batch_norm=False):
    layers = []
    in_channels = 3
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.Sequential(*layers)


class VGG(nn.Module):
    def __init__(self, in_size, num_classes, batch_norm=True, init_weights=True):
        super(VGG, self).__init__()
        self.features = make_layers(size_config[in_size][0], batch_norm=batch_norm)
        a, b, c = size_config[in_size][1]
        self.classifier = nn.Sequential(
            nn.Linear(a, b),
            nn.ReLU(True),
            nn.Dropout(),
            nn.Linear(b, c),
            nn.ReLU(True),
            nn.Dropout(),
            nn.Linear(c, num_classes),
        )
        if init_weights:
            self._initialize_weights()

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.constant_(m.bias, 0)
