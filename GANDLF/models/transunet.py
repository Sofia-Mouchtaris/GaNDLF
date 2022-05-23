# -*- coding: utf-8 -*-
"""
Implementation of TransUNet
"""
from torch.nn import ModuleList

from GANDLF.models.seg_modules.DownsamplingModule import DownsamplingModule
from GANDLF.models.seg_modules.EncodingModule import EncodingModule
from GANDLF.models.seg_modules.DecodingModule import DecodingModule
from GANDLF.models.seg_modules.UpsamplingModule import UpsamplingModule
from GANDLF.models.seg_modules.in_conv import in_conv
from GANDLF.models.seg_modules.out_conv import out_conv
from .modelBase import ModelBase
import sys
from GANDLF.utils.generic import checkPatchDimensions
from GANDLF.models.seg_modules.Interpolate import Interpolate
import torch
import torch.nn as nn
from torch.nn import ModuleList
import numpy as np
import math
from .unetr import _Transformer

class _DecoderCUP(nn.Sequential):
    def __init__(self, in_feats, out_feats, Norm, Conv, Upsample):
        super().__init__()

        self.conv = Conv(
                in_channels=in_feats,
                out_channels=out_feats,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False,
            )

        self.norm = Norm(out_feats)
        self.relu = nn.ReLU(inplace=True)
        self.upsample = Upsample

    def forward(self, x1, x2):
        if x2 is not None:
            x1 = torch.cat([x1, x2], dim=1)
        x = self.conv(x1)
        x = self.norm(x)
        x = self.relu(x)
        x = self.upsample(x)

        return x


class transunet(ModelBase):
    """
    This is the TransUNet architecture : https://doi.org/10.48550/arXiv.2102.04306. The Downsampling, Encoding modules
    are defined in the seg_modules file. These smaller modules are basically defined by 2 parameters, the input channels (filters) and the output channels (filters),
    and some other hyperparameters, which remain constant all the modules. For more details on the smaller modules please have a look at the seg_modules file.
    """

    def __init__(
        self,
        parameters: dict,
    ):
        super(transunet, self).__init__(parameters)

        if not ("depth" in parameters["model"]):
            parameters["model"]["depth"] = 4
            print("Default depth set to 4.")

        patch_check = checkPatchDimensions(
            parameters["patch_size"], numlay=parameters["model"]["depth"]
        )

        if patch_check != parameters["model"]["depth"] and patch_check >= 2:
            print(
                "The patch size is not large enough for desired depth. It is expected that each dimension of the patch size is divisible by 2^i, where i is in a integer greater than or equal to 2. Only the first %d layers will run."
                % patch_check
            )
        elif patch_check < 2:
            sys.exit(
                "The patch size is not large enough for desired depth. It is expected that each dimension of the patch size is divisible by 2^i, where i is in a integer greater than or equal to 2."
            )

        if not ("num_heads" in parameters["model"]):
            parameters["model"]["num_heads"] = 12
            print(
                "Default number of heads in multi-head self-attention (MSA) set to 12."
            )

        if not ("embed_dim" in parameters["model"]):
            parameters["model"]["embed_dim"] = 768
            print("Default size of embedded dimension set to 768.")

        self.num_heads = parameters["model"]["num_heads"]
        self.embed_size = parameters["model"]["embed_dim"]

        if self.embed_size % self.num_heads != 0:
            sys.exit(
                "The dimension of the embedding space must be divisible by the number of self-attention heads."
            )

        self.depth = patch_check

        if self.n_dimensions == 2:
            self.img_size = parameters["patch_size"][0:2]
            self.upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        elif self.n_dimensions == 3:
            self.img_size = parameters["patch_size"]
            self.upsample = nn.Upsample(scale_factor=2, mode='trilinear', align_corners=True)

        self.num_layers = 3 * self.depth  # number of transformer layers
        self.out_layers = [self.num_layers - 1]
        self.patch_dim = [i // 2 ** (self.depth) for i in self.img_size]

        self.ins = in_conv(
            input_channels=self.n_channels,
            output_channels=self.base_filters,
            conv=self.Conv,
            dropout=self.Dropout,
            norm=self.Norm,
        )

        self.ds = ModuleList([])
        self.en = ModuleList([])
        self.de = ModuleList([])

        for i_lay in range(0, self.depth):
            self.ds.append(
                DownsamplingModule(
                    input_channels=self.base_filters * 2 ** (i_lay),
                    output_channels=self.base_filters * 2 ** (i_lay + 1),
                    conv=self.Conv,
                    norm=self.Norm,
                )
            )

            self.de.append(
                _DecoderCUP(
                    in_feats= 2 * self.base_filters * 2 ** (i_lay + 1),
                    out_feats=self.base_filters * 2 ** (i_lay),
                    Conv=self.Conv,
                    Norm=self.Norm,
                    Upsample = self.upsample, 
                )
            )

            self.en.append(
                EncodingModule(
                    input_channels=self.base_filters * 2 ** (i_lay + 1),
                    output_channels=self.base_filters * 2 ** (i_lay + 1),
                    conv=self.Conv,
                    dropout=self.Dropout,
                    norm=self.Norm,
                )
            )

        self.transformer = _Transformer(
            img_size= [i // 2**(self.depth) for i in self.img_size],
            patch_size= 1,
            in_feats=self.base_filters * 2 ** self.depth,
            embed_size=self.embed_size,
            num_heads=self.num_heads,
            mlp_dim=2048,
            num_layers=self.num_layers,
            out_layers=self.out_layers,
            Conv=self.Conv,
            Norm=self.Norm,
        )

        self.transCUP = _DecoderCUP(
            in_feats=self.embed_size,
            out_feats=self.base_filters * 2 ** (self.depth-1),
            Conv=self.Conv,
            Norm=self.Norm,
            Upsample = self.upsample,
        )

        # TODO: conv 3x3 --> ReLU --> outconv
        self.out = out_conv(
            input_channels=self.base_filters,
            output_channels=self.n_classes,
            conv=self.Conv,
            norm=self.Norm,
            final_convolution_layer=self.final_convolution_layer,
            sigmoid_input_multiplier=self.sigmoid_input_multiplier,
        )

    def forward(self, x):
        """
        Parameters
        ----------
        x : Tensor
            Should be a 5D Tensor as [batch_size, channels, x_dims, y_dims, z_dims].

        Returns
        -------
        x : Tensor
            Returns a 5D Output Tensor as [batch_size, n_classes, x_dims, y_dims, z_dims].

        """
        y = []
        y.append(self.ins(x))

        # [downsample --> encode] x num layers
        for i in range(0, self.depth):
            temp = self.ds[i](y[i])
            y.append(self.en[i](temp))

        x = self.transformer(y[-1])[-1]
        x = x.transpose(-1, -2).view(-1, self.embed_size, *self.patch_dim)
        x = self.transCUP(x, None)

        # [upsample --> encode] x num layers
        for i in range(self.depth - 1, 0, -1):
            x = self.de[i-1](x, y[i])

        x = self.out(x)
        return x
