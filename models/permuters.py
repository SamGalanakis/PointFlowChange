import torch
from torch.distributions.transforms import Transform
from torch import nn
from torch.functional import einsum
from torch.nn import functional as F
import math
from functools import partial
from torch.distributions import Transform, constraints
from tqdm import tqdm
from utils import expm
from models.transform import Transform
import einops
import numpy as np

eps = 1e-8



class FullCombiner(Transform):
    def __init__(self,dim):
        super().__init__()
        self.dim = dim
        self.w = nn.Parameter(torch.Tensor(dim, dim))
        nn.init.orthogonal_(self.w)

    def forward(self,x,context=None):
        x = F.linear(x, self.w, bias=None)
        ldj  = torch.linalg.slogdet(self.w)[-1]
        return x,ldj
    
    def inverse(self,y,context=None):
        inv_mat = torch.linalg.inv(self.w)
        y = F.linear(y, inv_mat)
        return y

            

class ExponentialCombiner(Transform):
    def __init__(self,dim,algo='original',eps=1e-8,eps_expm=1e-8):
        super().__init__()
        self.dim = dim
        self.w = nn.Parameter(torch.randn((self.dim,self.dim)))
        self.scale = nn.Parameter(torch.ones(1) / 8)
        self.shift = nn.Parameter(torch.zeros(1))
        self.rescale = nn.Parameter(torch.ones(1))
        self.reshift = nn.Parameter(torch.zeros(1))
        self.algo = algo
        self.eps = eps
        self.eps_expm = eps_expm
 
    
    def forward(self,x,context=None):
        w_mat = self.rescale*torch.tanh(self.scale*self.w+self.shift) +self.reshift + self.eps
        return torch.matmul(expm(w_mat,eps=self.eps_expm,algo=self.algo),x.unsqueeze(-1)).squeeze(-1), w_mat.diagonal(dim1=-2,dim2=-1).sum()
    def inverse(self,y,context=None):
        w_mat = self.rescale*torch.tanh(self.scale*self.w+self.shift) +self.reshift + self.eps
        return torch.matmul(expm(-w_mat,eps= self.eps_expm,algo=self.algo),y.unsqueeze(-1)).squeeze(-1)

class Permuter(Transform):
    def __init__(self,permutation,event_dim=-1):
        super().__init__()
        
        self.register_buffer('permutation', permutation)
        self.event_dim = event_dim
        self.register_buffer('inv_permutation',torch.argsort(self.permutation))


    def forward(self,x,context=None):
        y = x.index_select(self.event_dim,self.permutation)
        return y, torch.zeros(x.size()[:self.event_dim], dtype=x.dtype, layout=x.layout, device=x.device)
    def inverse(self, y ,context=None):
        x = y.index_select(self.event_dim,self.inv_permutation)
        return x






class Reverse(Permuter):
    """
    Reverses inputs on a given dimension.
    Args:
        dim_size: int, number of elements on dimension dim
        dim: int, dimension to permute (excluding batch_dimension)
    """

    def __init__(self, dim_size, dim=1):
        super(Reverse, self).__init__(torch.arange(dim_size - 1, -1, -1), dim)




class LinearLU(Transform):
    """
    Linear bijection where the LU decomposition of the weights are parameterized.
    Similar to the LU version of the 1x1 convolution in [1].
    Costs:
        forward = O(BD^2)
        inverse = O(BD^2 + D)
        ldj = O(D)
    where:
        B = batch size
        D = number of features
    Args:
        num_features: int, Number of features in the input and output.
        identity_init: bool, if True initialize weights to be an identity matrix (default=True).
        bias: bool, if True a bias is included (default=False).
    References:
        [1] Glow: Generative Flow with Invertible 1×1 Convolutions,
            Kingma & Dhariwal, 2018, https://arxiv.org/abs/1807.03039
    """
    def __init__(self, num_features, identity_init=True, eps=1e-3, bias=False):
        super(LinearLU, self).__init__()
        self.num_features = num_features
        self.eps = eps

        self.lower_indices = np.tril_indices(num_features, k=-1)
        self.upper_indices = np.triu_indices(num_features, k=1)
        self.diag_indices = np.diag_indices(num_features)

        n_triangular_entries = ((num_features - 1) * num_features) // 2

        self.lower_entries = nn.Parameter(torch.zeros(n_triangular_entries))
        self.upper_entries = nn.Parameter(torch.zeros(n_triangular_entries))
        self.unconstrained_upper_diag = nn.Parameter(torch.zeros(num_features))

        if bias:
            self.bias = nn.Parameter(torch.Tensor(num_features))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters(identity_init)

    def reset_parameters(self, identity_init):
        self.identity_init = identity_init

        if self.bias is not None:
            nn.init.zeros_(self.bias)

        if identity_init:
            nn.init.zeros_(self.lower_entries)
            nn.init.zeros_(self.upper_entries)
            constant = np.log(np.exp(1 - self.eps) - 1)
            nn.init.constant_(self.unconstrained_upper_diag, constant)
        else:
            stdv = 1.0 / np.sqrt(self.num_features)
            nn.init.uniform_(self.lower_entries, -stdv, stdv)
            nn.init.uniform_(self.upper_entries, -stdv, stdv)
            nn.init.uniform_(self.unconstrained_upper_diag, -stdv, stdv)

    def _create_lower_upper(self):
        lower = self.lower_entries.new_zeros(self.num_features, self.num_features)
        lower[self.lower_indices[0], self.lower_indices[1]] = self.lower_entries
        # The diagonal of L is taken to be all-ones without loss of generality.
        lower[self.diag_indices[0], self.diag_indices[1]] = 1.

        upper = self.upper_entries.new_zeros(self.num_features, self.num_features)
        upper[self.upper_indices[0], self.upper_indices[1]] = self.upper_entries
        upper[self.diag_indices[0], self.diag_indices[1]] = self.upper_diag

        return lower, upper

    @property
    def upper_diag(self):
        return F.softplus(self.unconstrained_upper_diag) + self.eps

    def forward(self, x,context=None):
        L, U = self._create_lower_upper()
        t = F.linear(x, U)
        z = F.linear(t, L, self.bias)
        ldj = torch.sum(torch.log(self.upper_diag)).expand(x.shape[:2])
        return z, ldj

    def inverse(self, z,context=None):
        L, U = self._create_lower_upper()
        if self.bias is not None: z = z - self.bias
        t, _ = torch.triangular_solve(z.permute((0,2,1)), L, upper=False, unitriangular=True)
        t, _ = torch.triangular_solve(t, U, upper=True, unitriangular=False)
        x = t.permute((0,2,1))
        return x

    def weight(self):
        """Cost:
            weight = O(D^3)
        where:
            D = num of features
        """
        lower, upper = self._create_lower_upper()
        return lower @ upper

    def weight_inverse(self):
        """Cost:
            inverse = O(D^3)
        where:
            D = num of features
        """
        L, U = self._create_lower_upper()
        identity = torch.eye(self.num_features, self.num_features, device=self.unconstrained_upper_diag.device, dtype=self.unconstrained_upper_diag.dtype)
        lower_inverse, _ = torch.triangular_solve(identity, L, upper=False, unitriangular=True)
        weight_inverse, _ = torch.triangular_solve(lower_inverse, U, upper=True, unitriangular=False)
        return weight_inverse
if __name__ == '__main__':
    exp_comb = Exponential_combiner(6)
    learned_permuter = Learned_permuter(6)
    for i in range(100):
        a = torch.randn((20,2000,6))
        print(torch.max(torch.abs(a-learned_permuter._inverse(learned_permuter(a)))))

    # for i in tqdm(range(100)):
    #     x = torch.randn((20,6,6))
    #     y = exp_comb(x)
    #     x_ = exp_comb._inverse(y)
    #     print(torch.abs(x-x_).max())
    
