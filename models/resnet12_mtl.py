""" ResNet with MTL. """
import torch.nn as nn
from models.conv2d_mtl import Conv2dMtl
import torch.nn.functional as F
import torch
import learn2learn as l2l


def conv3x3mtl(in_planes, out_planes, stride=1):
    return Conv2dMtl(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class BasicBlock_MTL(nn.Module):
    expansion = 1
    
    def __init__(
            self,
            inplanes,
            planes,
            stride=1,
            downsample=None,
            drop_rate=0.0,
            drop_block=False,
            block_size=1,
    ):
        super(BasicBlock_MTL, self).__init__()
        self.conv1 = conv3x3mtl(inplanes, planes, stride=1)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.LeakyReLU(0.1)
        self.conv2 = conv3x3mtl(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = conv3x3mtl(planes, planes)
        self.bn3 = nn.BatchNorm2d(planes)
        self.maxpool = nn.MaxPool2d(stride)
        self.downsample = downsample
        self.stride = stride
        self.drop_rate = drop_rate
        self.num_batches_tracked = 0
        self.drop_block = drop_block
        self.block_size = block_size
        self.DropBlock = DropBlock(block_size=self.block_size)
    
    def forward(self, x):
        self.num_batches_tracked += 1
        
        residual = x
        
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        
        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)
        
        out = self.conv3(out)
        out = self.bn3(out)
        
        if self.downsample is not None:
            residual = self.downsample(x)
        out += residual
        out = self.relu(out)
        out = self.maxpool(out)
        
        if self.drop_rate > 0:
            if self.drop_block:
                feat_size = out.size()[2]
                keep_rate = max(
                    1.0 - self.drop_rate / 40000 * self.num_batches_tracked,
                    1.0 - self.drop_rate
                )
                gamma = (
                        (1 - keep_rate)
                        / self.block_size ** 2 * feat_size ** 2
                        / (feat_size - self.block_size + 1) ** 2
                )
                out = self.DropBlock(out, gamma=gamma)
            else:
                out = F.dropout(
                    out,
                    p=self.drop_rate,
                    training=self.training,
                    inplace=True,
                )
        return out


class DropBlock(nn.Module):
    def __init__(self, block_size):
        super(DropBlock, self).__init__()
        self.block_size = block_size
    
    def forward(self, x, gamma):
        
        if self.training:
            batch_size, channels, height, width = x.shape
            
            bernoulli = torch.distributions.Bernoulli(gamma)
            mask = bernoulli.sample((
                batch_size,
                channels,
                height - (self.block_size - 1),
                width - (self.block_size - 1),
            )).to(x.device)
            block_mask = self._compute_block_mask(mask)
            countM = (
                    block_mask.size(0)
                    * block_mask.size(1)
                    * block_mask.size(2)
                    * block_mask.size(3)
            )
            count_ones = block_mask.sum()
            return block_mask * x * (countM / count_ones)
        else:
            return x
    
    def _compute_block_mask(self, mask):
        left_padding = int((self.block_size - 1) / 2)
        right_padding = int(self.block_size / 2)
        
        batch_size, channels, height, width = mask.shape
        non_zero_idxs = mask.nonzero(as_tuple=False)
        nr_blocks = non_zero_idxs.shape[0]
        
        offsets = torch.stack(
            [
                torch.arange(self.block_size).view(-1, 1).expand(
                    self.block_size,
                    self.block_size).reshape(-1),
                torch.arange(self.block_size).repeat(self.block_size),
            ]
        ).t()
        offsets = torch.cat(
            (torch.zeros(self.block_size ** 2, 2).long(), offsets.long()),
            dim=1,
        ).to(mask.device)
        
        if nr_blocks > 0:
            non_zero_idxs = non_zero_idxs.repeat(self.block_size ** 2, 1)
            offsets = offsets.repeat(nr_blocks, 1).view(-1, 4)
            offsets = offsets.long()
            
            block_idxs = non_zero_idxs + offsets
            padded_mask = F.pad(
                mask,
                (left_padding, right_padding, left_padding, right_padding)
            )
            padded_mask[
                block_idxs[:, 0],
                block_idxs[:, 1],
                block_idxs[:, 2],
                block_idxs[:, 3]] = 1.0
        else:
            padded_mask = F.pad(
                mask,
                (left_padding, right_padding, left_padding, right_padding)
            )
        
        block_mask = 1 - padded_mask
        return block_mask


class ResNet12Backbone_MTL(nn.Module):
    
    def __init__(
            self,
            avg_pool=True,  # Set to False for 16000-dim embeddings
            wider=True,  # True mimics MetaOptNet, False mimics TADAM
            embedding_dropout=0.0,  # dropout for embedding
            dropblock_dropout=0.1,  # dropout for residual layers
            dropblock_size=5,
            channels=3,
    ):
        super(ResNet12Backbone_MTL, self).__init__()
        self.inplanes = channels
        self.Conv2d = Conv2dMtl
        block = BasicBlock_MTL
        if wider:
            num_filters = [64, 160, 320, 640]
        else:
            num_filters = [64, 128, 256, 512]
        
        self.layer1 = self._make_layer(
            block,
            num_filters[0],
            stride=2,
            dropblock_dropout=dropblock_dropout,
        )
        self.layer2 = self._make_layer(
            block,
            num_filters[1],
            stride=2,
            dropblock_dropout=dropblock_dropout,
        )
        self.layer3 = self._make_layer(
            block,
            num_filters[2],
            stride=2,
            dropblock_dropout=dropblock_dropout,
            drop_block=True,
            block_size=dropblock_size,
        )
        self.layer4 = self._make_layer(
            block,
            num_filters[3],
            stride=2,
            dropblock_dropout=dropblock_dropout,
            drop_block=True,
            block_size=dropblock_size,
        )
        if avg_pool:
            self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        else:
            self.avgpool = l2l.nn.Lambda(lambda x: x)
        self.embedding_dropout = embedding_dropout
        self.keep_avg_pool = avg_pool
        self.dropout = nn.Dropout(p=self.embedding_dropout, inplace=False)
        self.dropblock_dropout = dropblock_dropout
        
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
    
    def _make_layer(
            self,
            block,
            planes,
            stride=1,
            dropblock_dropout=0.0,
            drop_block=False,
            block_size=1,
    ):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                self.Conv2d(self.inplanes, planes * block.expansion,
                            kernel_size=1, stride=1, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )
        layers = []
        layers.append(block(
            self.inplanes,
            planes,
            stride,
            downsample,
            dropblock_dropout,
            drop_block,
            block_size)
        )
        self.inplanes = planes * block.expansion
        return nn.Sequential(*layers)
    
    def forward(self, x):
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.dropout(x)
        return x


class ResNet12_MTL(nn.Module):
    
    def __init__(
            self,
            output_size,
            hidden_size=640,  # miniImageNet-ImageNet images, used for the classifier
            avg_pool=True,  # Set to False for 16000-dim embeddings
            wider=True,  # True mimics MetaOptNet, False mimics TADAM
            embedding_dropout=0.0,  # dropout for embedding
            dropblock_dropout=0.1,  # dropout for residual layers
            dropblock_size=5,
            channels=3,
    ):
        super(ResNet12_MTL, self).__init__()
        self.features = ResNet12Backbone_MTL(
            avg_pool=avg_pool,
            wider=wider,
            embedding_dropout=embedding_dropout,
            dropblock_dropout=dropblock_dropout,
            dropblock_size=dropblock_size,
            channels=channels,
        )
        self.classifier = torch.nn.Linear(hidden_size, output_size)
    
    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x)
        return x


if __name__ == '__main__':
    model = ResNet12_MTL(output_size=5)
    for name, param in model.named_parameters():
        print(name, param.requires_grad)
    img = torch.randn(5, 3, 84, 84)
    model = model.to('cuda')
    img = img.to('cuda')
    out = model(img)
    print(out.shape)
