import torch
import torch.nn as nn

from models import (ActNormBijectionCloud, Augment, IdentityTransform,
                    PreConditionApplier, Reverse, Slice, transform)
from models.transform import Transform
import models
from utils import is_valid




class CouplingPreconditionerNoAttn(nn.Module):
    def __init__(self, event_dim=-1):
        super().__init__()

    def forward(self, x, context):

        return context


class CouplingPreconditionerAttn(nn.Module):
    def __init__(self, attn, pre_attention_mlp, x1_dim, event_dim=-1):
        super().__init__()
        self.attn = attn
        self.pre_attention_mlp = pre_attention_mlp
        self.event_dim = event_dim
        self.x1_dim = x1_dim

    def forward(self, x, context):
        x1, x2 = x.split([self.x1_dim, self.x1_dim], dim=self.event_dim)
        mlp_out = torch.utils.checkpoint.checkpoint(
            self.pre_attention_mlp, x1, preserve_rng_state=False)
        #attn_emb = self.attn(mlp_out,context)
        attn_emb = torch.utils.checkpoint.checkpoint(
            self.attn, mlp_out, context, preserve_rng_state=False)
        return attn_emb


def flow_block_helper(config,flow, attn,pre_attention_mlp, event_dim=-1):
    # CIF if aug>base latent dim else normal flow
    if config['input_dim'] < config['cif_latent_dim']:

        if config['global']:
            raise Exception('CIF + global embedding not implemented')

        return CIFblock(config,flow,attn,event_dim)
    elif config['input_dim'] ==  config['cif_latent_dim']:
        if  not config['global']:
            return PreConditionApplier(flow(config['input_dim'], config['attn_dim']), CouplingPreconditionerAttn(attn(), pre_attention_mlp(config['input_dim']//2), config['input_dim']//2, event_dim=event_dim))
        else:
            return flow(config['input_dim'], config['input_embedding_dim'])

    else:
        raise Exception('Augment dim smaller than main latent!')


class CIFblock(Transform):
    def __init__(self, config,flow,attn,event_dim):
        super().__init__()
        self.config = config
        self.event_dim = event_dim
        



        distrib_augment_net = models.MLP(config['latent_dim'],config['net_cif_dist_hidden_dims'],(config['cif_latent_dim']- config['latent_dim'])*2,nonlin=torch.nn.GELU())
        distrib_augment = models.ConditionalNormal(net =distrib_augment_net,split_dim = event_dim,clamp = config['clamp_dist'])
        self.act_norm = models.ActNormBijectionCloud(config['cif_latent_dim'])
        distrib_slice = distrib_augment
        self.augmenter = Augment(
            distrib_augment, config['latent_dim'], split_dim=event_dim)
        
        pre_attention_mlp = models.MLP(config['latent_dim']//2,config['pre_attention_mlp_hidden_dims'], config['attn_input_dim'], torch.nn.GELU(), residual=True)

        self.affine_cif = models.AffineCoupling(config['cif_latent_dim'],config['affine_cif_hidden'],nn.GELU(),scale_fn_type='sigmoid',split_dim=config['cif_latent_dim']-config['latent_dim'])
        self.flow = PreConditionApplier(flow(config['latent_dim'], config['attn_dim']), CouplingPreconditionerAttn(attn(), pre_attention_mlp, config['latent_dim']//2, event_dim=event_dim))
        self.slicer = Slice(distrib_slice, config['latent_dim'], dim=self.event_dim)
        
        self.reverse = models.Reverse(config['cif_latent_dim'],dim=-1)
        

    def forward(self, x, context=None):
        ldj_cif = torch.zeros(x.shape[:-1], device=x.device, dtype=x.dtype)

       

        x, ldj = self.augmenter(x, context=None)
        ldj_cif += ldj

        x,_ = self.reverse(x)

        x,ldj = self.affine_cif(x,context=None)
        ldj_cif += ldj

        x,ldj = self.act_norm(x)
        ldj_cif += ldj

        x,_ = self.reverse(x)
        
        x, ldj = self.slicer(x, context=None)
        ldj_cif += ldj


        x, ldj = self.flow(x, context=context)
        ldj_cif += ldj
        
        


        
        

        return x, ldj_cif

    def inverse(self, y, context=None):
        y = self.flow.inverse(y,context=context)
        y = self.slicer.inverse(y)
        y = self.reverse.inverse(y)
        y = self.act_norm.inverse(y)
        y = self.affine_cif.inverse(y)
        y = self.reverse.inverse(y)
        x = self.augmenter.inverse(y)


        return x
