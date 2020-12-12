import torch
import torch.nn as nn
from torch import distributions
from torch.optim import Adam, lr_scheduler
from torch.utils.data import DataLoader
import numpy as np
import os
from tqdm import tqdm
import wandb
from models.flow_modules import (
    CouplingLayer,
    AffineCouplingFunc,
    ConditionalNet,
    StraightNet,
)
from models.point_encoders import PointnetEncoder
from utils import loss_fun , loss_fun_ret, view_cloud

from data.datasets_pointflow import (
    CIFDatasetDecorator,
    ShapeNet15kPointClouds,
    CIFDatasetDecoratorMultiObject,
)

config_path = "config//config_train.yaml"
print(f"Loading config from {config_path}")
wandb.init(project="pointflowchange",config = config_path)





n_f= wandb.config['n_f'] 
n_f_k = wandb.config['n_f_k']
data_root_dir = wandb.config['data_root_dir']
save_model_path = wandb.config['save_model_path']
n_g = wandb.config['n_g']
n_g_k= wandb.config['n_g_k']
n_epochs = wandb.config['n_epochs']
sample_size = wandb.config['sample_size']
batch_size = wandb.config['batch_size']
x_noise = wandb.config['x_noise']
random_dataloader = wandb.config['random_dataloader']
emb_dim =  wandb.config['emb_dim']
categories = wandb.config['categories']
lr= wandb.config['lr']

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

print(f'Using device {device}')


prior_z = distributions.MultivariateNormal(
    torch.zeros(3), torch.eye(3)
)

prior_e = distributions.MultivariateNormal(
    torch.zeros(emb_dim), torch.eye(emb_dim)
)

cloud_pointflow = ShapeNet15kPointClouds(
    tr_sample_size=sample_size,
    te_sample_size=sample_size,
    root_dir= data_root_dir,
  
    normalize_per_shape=False,
    normalize_std_per_axis=False,
    split="train",
    scale=1.0,
    categories=categories,
    random_subsample=True,
)



if random_dataloader:
        cloud_pointflow = CIFDatasetDecoratorMultiObject(
            cloud_pointflow, sample_size
        )
        batch_size = batch_size 
dataloader_pointflow = DataLoader(
    cloud_pointflow, batch_size=batch_size, shuffle=True
)


# Prepare models


pointnet = PointnetEncoder(emb_dim,input_dim=3).to(device)




# for f
f_blocks = [[] for x in range(n_f)]
f_permute_list_list = [[2,0,1]]*n_f_k
f_split_index_list = [1]*len(f_permute_list_list)

for i in range(n_f):
    for j in range(n_f_k):
        split_index = f_split_index_list[j]
        permute_tensor = torch.LongTensor([f_permute_list_list[j] ]).to(device)  

        mutiply_func = ConditionalNet(emb_dim=emb_dim,in_dim=split_index)
        add_func  = ConditionalNet(emb_dim=emb_dim,in_dim = split_index)
        coupling_func = AffineCouplingFunc(mutiply_func,add_func)
        coupling_layer = CouplingLayer(coupling_func,split_index,permute_tensor)
        f_blocks[i].append(coupling_layer)

# for g
g_blocks = [[] for x in range(n_g)]
g_permute_list_list = [list(range(emb_dim//2,emb_dim))+list(range(emb_dim//2))]*n_g_k
g_split_index_list = [emb_dim//2]*len(g_permute_list_list)

for i in range(n_g):
    for j in range(n_g_k):
        split_index = g_split_index_list[j]
        permute_tensor = torch.LongTensor([g_permute_list_list[j] ]).to(device)        
        mutiply_func = StraightNet(in_dim = split_index)
        add_func  = StraightNet(split_index)
        coupling_func = AffineCouplingFunc(mutiply_func,add_func)
        coupling_layer = CouplingLayer(coupling_func,split_index,permute_tensor)
        g_blocks[i].append(coupling_layer)        
   

model_dict = {'pointnet':pointnet}
for i, f_block in enumerate(f_blocks):
    for k in range(len(f_block)):
        model_dict[f'f_block_{i}_{k}'] = f_block[k]

for i, g_block in enumerate(g_blocks):
     for k in range(len(g_block)):
        model_dict[f'g_block_{i}_{k}'] = g_block[k]

all_params = []
for model_part in model_dict.values():
    #Send to device before passing to optimizer
    model_part.to(device)
    #Add model to watch list
    wandb.watch(model_part)
    model_part.train()
    all_params += model_part.parameters()
optimizer = Adam(all_params,lr=lr)

scheduler = lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.8)

for epoch in tqdm(range(n_epochs)):
    loss_acc_z = 0
    loss_acc_e = 0

    optimizer.zero_grad()
    for index, batch in enumerate(tqdm(dataloader_pointflow)):
        
        batch = cloud_pointflow[5]
    
        # The sampling that goes through pointnet
        embs_tr_batch = batch["train_points"].to(device)

        # The sampling that goes through f conditioned on e generated by the previous sampling
        tr_batch = batch['points_to_decode']

        #Add noise to tr_batch:
        tr_batch = tr_batch.float() + x_noise * torch.rand(tr_batch.shape)
        tr_batch = tr_batch.to(device)
        #Store before reshaping
        num_points_per_object = tr_batch.shape[1]
        #Squashing each shape into one dimension
        tr_batch = tr_batch.to(device).reshape((-1, 3)) #get back to this
        
                

        #Pass through pointnet
        w = pointnet(embs_tr_batch)
        w = w.unsqueeze(dim=1).expand([w.shape[0],num_points_per_object,w.shape[-1]]).reshape(-1,w.shape[-1])


        #Pass pointnet embedding through g flow and keep track of determinant
        e_ldetJ = 0
        e = w
        for g_block in g_blocks:
            for g_layer in g_block:
                e, inter_e_ldetJ = g_layer(e)
                e_ldetJ += inter_e_ldetJ
        
        #Pass pointcloud through f flow conditioned on e and keep track of determinant
        z_ldetJ=0
        z = tr_batch
        for f_block in f_blocks:
            for f_layer in f_block:
                
               
                z, inter_z_ldetJ = f_layer(z,e)
                z_ldetJ += inter_z_ldetJ
        
        
        loss_z, loss_e = loss_fun(
                z,
                z_ldetJ,
                prior_z,
                e,
                e_ldetJ,
                prior_e,
            )
        loss = loss_e + loss_z
        loss_acc_z += loss_z.item()
        loss_acc_e += loss_e.item()
        wandb.log({'loss': loss, 'loss_z': loss_z,'loss_e': loss_e})
        loss.backward()
        optimizer.step()
    # Adjust lr according to epoch
    scheduler.step()
    if epoch // 10 == 0:
        
        save_state_dict = {key:val.state_dict() for key,val in model_dict.items()}
        save_state_dict['optimizer'] = optimizer.state_dict()
        save_state_dict['scheduler'] = optimizer.state_dict()
        save_model_path = save_model_path+ f"_{epoch}_" +wandb.run.name+".pt"
        print(f"Saving model to {save_model_path}")
        torch.save(save_state_dict,save_model_path)
    #wandb.save(save_model_path) # File seems to be too big for autosave
    