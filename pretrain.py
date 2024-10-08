import argparse

import numpy as np
import torch
import os.path as osp

from metrics import compute_differential_entropy, compute_mutual_information, compute_precision, ROC_OOD
from models.mtl import MtlLearner
from utils.misc import pprint, ECELoss, count_acc
from utils.gpu_tools import set_gpu
from trainer.meta import MetaTrainer
from trainer.pre import PreTrainer

parser = argparse.ArgumentParser()
# Basic parameters
parser.add_argument('--model_type', type=str, default='ResNet', choices=['ResNet'])  # The network architecture
parser.add_argument('--dataset', type=str, default='miniImageNet',
                    choices=['miniImageNet', 'CIFAR-FS', 'FC100'])  # Dataset
parser.add_argument('--phase', type=str, default='pre_train',
                    choices=['pre_train', 'meta_train', 'meta_eval'])  # Phase
parser.add_argument('--seed', type=int, default=0)  # Manual seed for PyTorch, "0" means using random seed
parser.add_argument('--gpu', default='0')  # GPU id

# Parameters for meta-train phase
parser.add_argument('--max_epoch', type=int, default=100)  # Epoch number for meta-train phase
parser.add_argument('--num_batch', type=int, default=100)  # The number for different tasks used for meta-train
parser.add_argument('--way', type=int, default=5)  # Way number, how many classes in a task
parser.add_argument('--shot', type=int, default=5)  # Shot number, how many samples for one class in a task
parser.add_argument('--query', type=int, default=15)  # The number of test samples for each class in a task
parser.add_argument('--meta_lr1', type=float, default=0.0001)  # Learning rate for SS weights
parser.add_argument('--meta_lr2', type=float, default=0.001)  # Learning rate for FC weights
parser.add_argument('--base_lr', type=float, default=0.01)  # Learning rate for the inner loop
parser.add_argument('--update_step', type=int, default=100)  # The number of updates for the inner loop
parser.add_argument('--step_size', type=int, default=10)  # The number of epochs to reduce the meta learning rates
parser.add_argument('--gamma', type=float, default=0.5)  # Gamma for the meta-train learning rate decay

# Parameters for pretain phase
parser.add_argument('--pre_max_epoch', type=int, default=110)  # Epoch number for pre-train phase
parser.add_argument('--pre_batch_size', type=int, default=128)  # Batch size for pre-train phase
parser.add_argument('--pre_lr', type=float, default=0.1)  # Learning rate for pre-train phase
parser.add_argument('--pre_gamma', type=float, default=0.2)  # Gamma for the pre-train learning rate decay
parser.add_argument('--pre_step_size', type=int,
                    default=30)  # The number of epochs to reduce the pre-train learning rate
parser.add_argument('--pre_custom_momentum', type=float, default=0.9)  # Momentum for the optimizer during pre-train
parser.add_argument('--pre_custom_weight_decay', type=float,
                    default=0.0005)  # Weight decay for the optimizer during pre-train

# Set and print the parameters
args = parser.parse_args()
pprint(vars(args))

# Set the GPU id
set_gpu(args.gpu)

# Set manual seed for PyTorch
if args.seed == 0:
    print('Using random seed.')
    torch.backends.cudnn.benchmark = True
else:
    print('Using manual seed:', args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

# Start trainer for pre-train, meta-train or meta-eval
trainer = PreTrainer(args)
trainer.train()
