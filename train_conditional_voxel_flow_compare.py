import torch
import pyro
import pyro.distributions as dist
import pyro.distributions.transforms as T
import os
import numpy as np
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from utils import load_las, random_subsample,view_cloud_plotly,grid_split,extract_area,co_min_max,collate_voxel
from torch.utils.data import Dataset,DataLoader
from itertools import permutations, combinations
from tqdm import tqdm
from models.pytorch_geometric_pointnet2 import Pointnet2
from models.nets import ConditionalDenseNN, DenseNN
from models.voxel_cnn import VoxelCNN
from torch_geometric.data import Data,Batch
from torch_geometric.nn import fps
from dataloaders import ConditionalDataGrid, ShapeNetLoader, ConditionalVoxelGrid
import wandb
import torch.multiprocessing as mp
from torch.nn.parallel import DataParallel
import torch.distributed as distributed
from models.permuters import Full_matrix_combiner,Exponential_combiner,Learned_permuter
from models.batchnorm import BatchNorm
from torch.autograd import Variable, Function
from models.Exponential_matrix_flow import conditional_exponential_matrix_coupling
from models.gcn_encoder import GCNEncoder
import torch.multiprocessing as mp
from torch_geometric.nn import DataParallel as geomDataParallel
from torch import nn
import functools
from models.flow_creator import Conditional_flow_layers

def main(rank, world_size):




    dirs = [r'/mnt/cm-nas03/synch/students/sam/data_test/2018',r'/mnt/cm-nas03/synch/students/sam/data_test/2019',r'/mnt/cm-nas03/synch/students/sam/data_test/2020']
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    print(f'Using device {device}!')
    config_path = r"config/config_conditional_voxel.yaml"
    wandb.init(project="flow_change",config = config_path)
    config = wandb.config
    sample_size= config['sample_size'] 
    n_flow_layers = config['n_flow_layers']
    early_stop_margin = config['early_stop_margin']
    hidden_dims = config['hidden_dims']
    save_model_path = config['save_model_path']
    count_bins =config['count_bins']
    input_dim = config['input_dim']
    batch_size = wandb.config['batch_size']
    grid_square_size = config['grid_square_size']
    clearance = config['clearance']
    subsample = config['subsample']
    patience = config['patience']
    preload = config['preload']
    min_points = config['min_points']
    n_epochs = config['n_epochs']
    lr = config['lr']
    num_workers = config['num_workers']
    permuter_type = config['permuter_type']
    flow_type = config['flow_type']
    batchnorm = config['batchnorm']
    optimizer_type = config['optimizer_type']
    batchnorm_encodings = config['batchnorm_encodings']
    encoder_type = config['encoder_type']
    weight_decay = config['weight_decay']
    data_parallel = config['data_parallel']
    data_loader = config['data_loader']
    voxel_size = config['voxel_size']
    global_emb_dim = config['global_emb_dim']
    voxel_emb_dim = config['voxel_emb_dim']

    n_voxels = int((1/voxel_size)**3)
    torch.backends.cudnn.benchmark = True
    
    
    one_up_path = os.path.dirname(__file__)
    out_path = os.path.join(one_up_path,r"save/processed_dataset")
    dataset = ConditionalVoxelGrid(dirs,out_path=out_path,grid_square_size=grid_square_size,clearance=clearance,preload=preload,min_points=min_points)
   
    shuffle=True
    #SET PIN MEM TRUE
    collate = functools.partial(collate_voxel,voxel_size=voxel_size,input_dim=input_dim)
    dataloader = DataLoader(dataset,shuffle=shuffle,batch_size=batch_size,num_workers=num_workers,collate_fn=collate,pin_memory=False,prefetch_factor=2)







    if flow_type == 'exponential_coupling':
        flow = lambda  : conditional_exponential_matrix_coupling(input_dim=input_dim, context_dim=context_dim, hidden_dims=hidden_dims, split_dim=None, dim=-1,device='cpu')
    elif flow_type == 'spline_coupling':
        flow = lambda : T.conditional_spline(input_dim=input_dim, context_dim=context_dim, hidden_dims=hidden_dims,count_bins=count_bins,bound=3.0)
    elif flow_type == 'spline_autoregressive':
        flow = lambda : T.conditional_spline_autoregressive(input_dim=input_dim, context_dim=context_dim, hidden_dims=hidden_dims,count_bins=count_bins,bound=3)
    elif flow_type == 'affine_coupling':
        flow = lambda : T.conditional_affine_coupling(input_dim=input_dim, context_dim=context_dim, hidden_dims=hidden_dims)
    else:
        raise Exception(f'Invalid flow type: {flow_type}')
    if permuter_type == 'Exponential_combiner':
        permuter = lambda : Exponential_combiner(input_dim)
    elif permuter_type == 'Learned_permuter':
        permuter = lambda : Learned_permuter(input_dim)
    elif permuter_type == 'Full_matrix_combiner':
        permuter = lambda : Full_matrix_combiner(input_dim)
    elif permuter_type == "random_permute":
        permuter = lambda : T.Permute(torch.randperm(input_dim, dtype=torch.long).to(device))
    else:
        raise Exception(f'Invalid permuter type: {permuter_type}')



    
    flow_input_dim = voxel_emb_dim
    context_dim = global_emb_dim
    base_dist = dist.Normal(torch.zeros(flow_input_dim).to(device), torch.ones(flow_input_dim).to(device))
    conditional_flow_layers = Conditional_flow_layers(flow,n_flow_layers,flow_input_dim,context_dim,count_bins,device,permuter,hidden_dims,batchnorm)

    
    parameters=[]
    #VOXEL ENCODER
    if encoder_type == 'pointnet2':
        encoder = Pointnet2(feature_dim=input_dim-3,out_dim=voxel_emb_dim)
    elif encoder_type == 'gcn':
        encoder = GCNEncoder(in_dim= input_dim,out_channels=voxel_emb_dim,k=20)
    else:
        raise Exception('Invalid encoder type!')
    if data_parallel:
        encoder = geomDataParallel(encoder).to(device)
    else:
        encoder = encoder.to(device)
    
    parameters+= encoder.parameters()
    wandb.watch(encoder,log_freq=10)

    #FULL GRID ENCODER

    grid_encoder = VoxelCNN(input_dim=voxel_emb_dim,emb_dim=global_emb_dim)
    if data_parallel:
        grid_encoder = nn.DataParallel(grid_encoder).to(device)
    else:
        grid_encoder = grid_encoder.to(device)
    
    parameters += grid_encoder.parameters()

    wandb.watch(grid_encoder,log_freq=10)

    #BATCHNORM ON FULL GRID ENCODER
    if batchnorm_encodings:
        batchnorm_encoder = torch.nn.BatchNorm1d(context_dim)
        if data_parallel:
            batchnorm_encoder = nn.DataParallel(batchnorm_encoder).to(device)
        else:
            batchnorm_encoder = batchnorm_encoder.to(device)
        parameters+= batchnorm_encoder.parameters()
    

    transformations = conditional_flow_layers.transformations
    

    for transform in transformations:
        if isinstance(transform,torch.nn.Module):
            transform.train()
            if data_parallel:
                transform = nn.DataParallel(transform).to(device)
            else:
                transforms = transform.to(device)
            parameters+= transform.parameters()
            wandb.watch(transform,log_freq=10)



    flow_dist = dist.ConditionalTransformedDistribution(base_dist, transformations)
    
    if optimizer_type =='Adam':
        optimizer = torch.optim.Adam(parameters, lr=lr,weight_decay=weight_decay) 
    elif optimizer_type == 'Adamax':
        optimizer = torch.optim.Adamax(parameters, lr=lr)
    elif optimizer_type == 'AdamW':
        optimizer = torch.optim.AdamW(parameters, lr=lr,weight_decay=weight_decay)
    else:
        raise Exception('Invalid optimizer type!')

    

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,factor=0.05,patience=patience,threshold=0.0001)
    save_model_path = r'save/conditional_flow_compare'
    

    

    
    torch.autograd.set_detect_anomaly(False)
    for epoch in range(n_epochs):
        print(f"Starting epoch: {epoch}")
        for batch_ind,batch in enumerate(tqdm(dataloader)):
            

            optimizer.zero_grad()
            batch = [x.to(device) for x in batch]
            batch_0,batch_0_voxels,batch_sample_0,voxel_cluster_0,batch_1,batch_1_voxels,batch_sample_1,voxel_cluster_1 = batch
            batch_0.batch = voxel_cluster_0
            batch_1.batch = voxel_cluster_1

            encodings_0 = encoder(batch_0)
            encodings_1 = encoder(batch_1)
            
            
            empty_grid_0 = torch.zeros((batch_size,n_voxels,n_voxels,n_voxels,voxel_emb_dim),dtype=torch.float32,device=device)
            conditioned = flow_dist.condition(encodings.unsqueeze(-2))
            
           
            loss = -conditioned.log_prob(extract_1).mean()


            


            assert not loss.isnan(), "Nan loss!"
            loss.backward()
            torch.nn.utils.clip_grad_norm_(parameters,max_norm=2.0)
            
            optimizer.step()
            
            flow_dist.clear_cache()
            
            scheduler.step(loss)
            current_lr = optimizer.param_groups[0]['lr']
            if batch_ind!=0 and  (batch_ind % int(len(dataloader)/100)  == 0):
                print(f'Making samples and saving!')
                with torch.no_grad():
                    cond_nump,gen_sample = numpy_samples(conditioned,data_list_0)
                    wandb.log({'loss':loss.item(),"Cond_cloud": wandb.Object3D(cond_nump),"Gen_cloud": wandb.Object3D(gen_sample),'lr':current_lr})
                    save_dict = {"optimizer_dict": optimizer.state_dict(),'encoder_dict':encoder.state_dict(),'batchnorm_encoder_dict':batchnorm_encoder.state_dict(),'flow_transformations':conditional_flow_layers.make_save_list()}
                    torch.save(save_dict,os.path.join(save_model_path,f"{epoch}_{batch_ind}_model_dict.pt"))
            else:
                wandb.log({'loss':loss.item(),'lr':current_lr})
                
            
            
if __name__ == "__main__":
    world_size = torch.cuda.device_count()
    print('Let\'s use', world_size, 'GPUs!')
    rank=''
    main(rank,world_size)