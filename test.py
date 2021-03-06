from dataloaders import AmsVoxelLoader
from utils import config_loader
from tqdm import tqdm

config = config_loader('config/config.yaml')
device = 'cpu'
# dataset = AmsVoxelLoader(config['directory_path_train'],config['directory_path_test'], out_path='save/processed_dataset', preload=True,
#         n_samples = config['sample_size']final_voxel_size = config['final_voxel_size'],device=device,
#         n_samples_context = config['n_samples_context'], context_voxel_size = config['context_voxel_size'],mode='train',
#         getter_mode = 'all')


dataset = AmsVoxelLoader(config['directory_path_train'],config['directory_path_test'], out_path='save/processed_dataset', preload=True,
        n_samples = config['sample_size'],final_voxel_size = config['final_voxel_size'],device=device,
         n_samples_context = config['n_samples_context'], context_voxel_size = config['context_voxel_size'],mode='test',
         getter_mode = 'all')

for x in tqdm(range(len(dataset))):
    dataset[x]
print(len(dataset))
