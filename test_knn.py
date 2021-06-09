"""
=================================
K-NN classification - PyTorch API
=================================

The :mod:`.argKmin(K)` reduction supported by KeOps :class:`pykeops.torch.LazyTensor` allows us
to perform **bruteforce k-nearest neighbors search** with four lines of code.
It can thus be used to implement a **large-scale** 
`K-NN classifier <https://en.wikipedia.org/wiki/K-nearest_neighbors_algorithm>`_,
**without memory overflows**.



"""

#####################################################################
# Setup
# -----------------
# Standard imports:

import time
from models import DGCNNembedder,DGCNN,PointNet2SSGSeg
import numpy as np
import torch
from utils import count_parameters


use_cuda = True 
dtype = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor

points = torch.randn((1000,1000, 6)).cuda()

model = PointNet2SSGSeg(out_mlp_dims=[5,5,5]).cuda()

out = model(points)
pass


