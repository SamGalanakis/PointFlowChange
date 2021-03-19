import torch
import pyro
import pyro.distributions as dist
import pyro.distributions.transforms as T
import os
import numpy as np
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from utils import load_las, random_subsample,view_cloud_plotly,grid_split,extract_area,co_min_max,feature_assigner,Adamax,Early_stop
from torch.utils.data import Dataset,DataLoader
from itertools import permutations, combinations
from tqdm import tqdm
from models.pytorch_geometric_pointnet2 import Pointnet2
from models.nets import ConditionalDenseNN, DenseNN
from torch_geometric.data import Data,Batch
from torch_geometric.nn import fps
from dataloaders import ConditionalDataGrid, ShapeNetLoader, ConditionalVoxelGrid,ChallengeDataset
import wandb
import torch.multiprocessing as mp
from torch.nn.parallel import DataParallel
import torch.distributed as distributed
from models.permuters import Full_matrix_combiner,Exponential_combiner,Learned_permuter
from models.batchnorm import BatchNorm
from torch.autograd import Variable, Function
from models.Exponential_matrix_flow import exponential_matrix_coupling
from models.gcn_encoder import GCNEncoder
import torch.multiprocessing as mp
from torch_geometric.nn import DataParallel as geomDataParallel
from torch import nn
from models.flow_creator import Conditional_flow_layers
import argparse
from straight_challenge_classifier import log_prob_to_change
from time import time

def load_transformations(load_dict,conditional_flow_layers):
    for transformation_params,transformation in zip(load_dict['flow_transformations'],conditional_flow_layers.transformations):
        if isinstance(transformation,nn.Module):
            transformation.load_state_dict(transformation_params)
        elif isinstance(transformation,pyro.distributions.pyro.distributions.transforms.Permute):
            transformation.permutation = transformation_params
        else:
            raise Exception('How to load?')
    
    return conditional_flow_layers



def initialize_straight_model(config,device = 'cuda',mode='train'):
    flow_input_dim = config['input_dim']
    flow_type = config['flow_type']
    permuter_type = config['permuter_type']
    hidden_dims = config['hidden_dims']
    data_parallel = config['data_parallel']
    parameters = []
    if config['coupling_block_nonlinearity']=="ELU":
        coupling_block_nonlinearity = nn.ELU()
    elif config['coupling_block_nonlinearity']=="RELU":
        coupling_block_nonlinearity = nn.ReLU()
    else:
        raise Exception("Invalid coupling_block_nonlinearity")



    if flow_type == 'exponential_coupling':
        flow = lambda  : exponential_matrix_coupling(input_dim=flow_input_dim, hidden_dims=hidden_dims, split_dim=None, dim=-1,device='cpu',nonlinearity=coupling_block_nonlinearity)
    elif flow_type == 'spline_coupling':
        flow = lambda : T.spline_coupling(input_dim=flow_input_dim, hidden_dims=hidden_dims,count_bins=config["count_bins"],bound=3.0)
    elif flow_type == 'spline_autoregressive':
        flow = lambda : T.spline_autoregressive(input_dim=flow_input_dim, hidden_dims=hidden_dims,count_bins=count_bins,bound=3)
    elif flow_type == 'affine_coupling':
        flow = lambda : T.affine_coupling(input_dim=flow_input_dim, hidden_dims=hidden_dims)
    else:
        raise Exception(f'Invalid flow type: {flow_type}')
    if permuter_type == 'Exponential_combiner':
        permuter = lambda : Exponential_combiner(flow_input_dim)
    elif permuter_type == 'Learned_permuter':
        permuter = lambda : Learned_permuter(flow_input_dim)
    elif permuter_type == 'Full_matrix_combiner':
        permuter = lambda : Full_matrix_combiner(flow_input_dim)
    elif permuter_type == "random_permute":
        permuter = lambda : T.Permute(torch.randperm(flow_input_dim, dtype=torch.long).to(device))
    else:
        raise Exception(f'Invalid permuter type: {permuter_type}')

    conditional_flow_layers = Conditional_flow_layers(flow,config['n_flow_layers'],flow_input_dim,device,permuter,config['hidden_dims'],config['batchnorm'])

    for transform in conditional_flow_layers.transformations:
        if isinstance(transform,torch.nn.Module):
            if mode == 'train':
                transform.train()
            else:
                transform.eval()
            if data_parallel:
                transform = nn.DataParallel(transform).to(device)
            else:
                transform = transform.to(device)
            parameters+= transform.parameters()
            #wandb.watch(transform,log_freq=10)




    return {'parameters':parameters,"flow_layers":conditional_flow_layers}

def collate_straight(batch):
        return batch[0]

def train_straight_pair(parameters,transformations,config,extract_0,extract_1,device):
    
    start_time = time()
    extract_0,extract_1 = extract_0.to(device),extract_1.to(device)
    base_dist = dist.Normal(torch.zeros(config["input_dim"]).to(device), torch.ones(config["input_dim"]).to(device))
    flow_dist = dist.TransformedDistribution(base_dist, transformations)
    
    if config["optimizer_type"] =='Adam':
        optimizer = torch.optim.Adam(parameters, lr=config["lr"],weight_decay=config["weight_decay"]) 
    elif config["optimizer_type"] == 'Adamax':
        optimizer = Adamax(parameters, lr=config["lr"],weight_decay=config["weight_decay"],polyak =  0.999)
    elif config["optimizer_type"] == 'AdamW':
        optimizer = torch.optim.AdamW(parameters, lr=config["lr"],weight_decay=config["weight_decay"])
    elif config["optimizer_type"] == 'SGD':
        optimizer = torch.optim.SGD(parameters, lr=config["lr"], momentum=0.9)
    else:
        raise Exception('Invalid optimizer type!')

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,factor=0.5,patience=config["patience"],threshold=0.0001,min_lr=config["min_lr"])
    
    early_stopper = Early_stop(patience=config["patience_stopper"],min_perc_improvement=config['early_stop_margin'])
    for epoch in range(config["n_epochs"]):

        optimizer.zero_grad()
        input_data =  random_subsample(extract_0.squeeze(),config['points_per_batch']).unsqueeze(0)
        input_with_noise = torch.randn_like(input_data,device=device)*(0.001) + input_data
        loss = -flow_dist.log_prob(input_with_noise.squeeze()).mean()
        assert not loss.isnan()
        
        loss.backward()
        optimizer.step()
        
    
        scheduler.step(loss)

    
        stop_train = early_stopper.log(loss.cpu())
        flow_dist.clear_cache()
        if stop_train:
            print(f"Early stopped at epoch: {epoch}!")
            break
   


    with torch.no_grad():
        log_prob_0 = flow_dist.log_prob(extract_0).squeeze()
        y =extract_1
        for transform in reversed(flow_dist.transforms):
            x = transform.inv(y)
            y = x
        full_probs = base_dist.log_prob(y)
        log_prob_1 = flow_dist.log_prob(extract_1).squeeze()


    duration = time() - start_time
    print(f"Took: {duration}")
    return log_prob_0,log_prob_1,full_probs




def straight_train(args):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    one_up_path = os.path.dirname(__file__)
    out_path = os.path.join(one_up_path,r"save/processed_dataset")
    
    config_path = r"config/config_straight.yaml"
    if args.WANDB_MODE == "dryrun":
        os.environ['WANDB_MODE'] = 'dryrun'
    wandb.init(project="flow_change",config = config_path)
    config = wandb.config
    
    torch.backends.cudnn.benchmark = True
 
    
    


    one_up_path = os.path.dirname(__file__)
    out_path = os.path.join(one_up_path,r"save/processed_dataset")
    dirs = [config['dir_challenge']+year for year in ["2016","2020"]]
    if config['data_loader'] == 'ConditionalDataGridSquare':
        dataset=ConditionalDataGrid(config['dirs_challenge'],out_path=out_path,preload=config['preload'],subsample=config["subsample"],sample_size=config["sample_size"],min_points=config["min_points"],grid_type='square',normalization=config['normalization'],grid_square_size=config['grid_square_size'])
    elif config['data_loader'] == 'ConditionalDataGridCircle':
        dataset=ConditionalDataGrid(config['dirs_challenge'],out_path=out_path,preload=config['preload'],subsample=config['subsample'],sample_size=config['sample_size'],min_points=config['min_points'],grid_type='circle',normalization=config['normalization'],grid_square_size=config['grid_square_size'])
    elif config['data_loader']=='ShapeNet':
        dataset = ShapeNetLoader(r'D:\data\ShapeNetCore.v2.PC15k\02691156\train',out_path=out_path,preload=config['preload'],subsample=config['subsample'],sample_size=config['sample_size'])
    elif config['data_loader']=='ChallengeDataset':
        if args.start_index is not None:
                subset = range(args.start_index,args.end_index)
            
            
        else:
            subset = None
        dataset = ChallengeDataset(config['dirs_challenge_csv'], dirs, out_path,subsample="fps",sample_size=config['sample_size'],preload=config['preload'],normalization=config['normalization'],subset=subset,radius=config['radius'],remove_ground=config['remove_ground'],mode = args.mode)
    else:
        raise Exception('Invalid dataloader type!')
    dataloader = DataLoader(dataset,shuffle=False,batch_size=config['batch_size'],num_workers=config["num_workers"],collate_fn=collate_straight,pin_memory=True,prefetch_factor=2,drop_last=False)



    
    torch.autograd.set_detect_anomaly(False)
    
    for index, batch in enumerate(tqdm(dataloader)):
        print(f"Starting forward!")
        extract_0, extract_1, label, idx = batch
        extract_0,extract_1 = extract_0.to(device),extract_1.to(device)
        extract_0 , extract_1 = extract_0[:,:config['input_dim']].unsqueeze(0),extract_1[:,:config['input_dim']].unsqueeze(0)
        #Initialize models
        models_dict = initialize_straight_model(config,device,mode='train')
        parameters = models_dict['parameters']
        conditional_flow_layers = models_dict['flow_layers']
        transformations = conditional_flow_layers.transformations
        log_prob_0_given_0,log_prob_1_given_0,full_probs_1_given_0 = train_straight_pair(parameters,transformations,config,extract_0,extract_1,device=device)
   

        

        print(f"Starting reverse!")
        if config['reinitialize_before_reverse']:
            models_dict = initialize_straight_model(config,device,mode='train')
            parameters = models_dict['parameters']
            conditional_flow_layers = models_dict['flow_layers']
            transformations = conditional_flow_layers.transformations
        log_prob_1_given_1,log_prob_0_given_1,full_probs_0_given_1 = train_straight_pair(parameters,transformations,config,extract_1,extract_0,device=device)

        
        

        change_features_dict= {
        "idx":idx,
        "log_prob_0_given_0": log_prob_0_given_0,
        "log_prob_1_given_0": log_prob_1_given_0,
        "full_probs_1_given_0": full_probs_1_given_0,

        "log_prob_1_given_1": log_prob_1_given_1,
        "log_prob_0_given_1": log_prob_0_given_1,
        "full_probs_0_given_1": full_probs_0_given_1,
        "label" : label.item()
        }

        torch.save(change_features_dict,os.path.join(os.path.join(out_path,"straight_features"),f"{args.mode}_{args.run_name}_{idx}_direct_change_features.pt"))
                
            
            
if __name__ == "__main__":
    world_size = torch.cuda.device_count()
    print('Let\'s use', world_size, 'GPUs!')
    rank=''
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_name",const="arun",nargs='?')
    parser.add_argument("--start_index",type=int)
    parser.add_argument("--end_index",type=int)
    parser.add_argument("--WANDB_MODE",const = 'dryrun',nargs='?')
    parser.add_argument("--mode",const = 'train',nargs='?')
    args = parser.parse_args()
    straight_train(args)