import argparse

import numpy as np
import torch
from torch.utils.data import DataLoader

from dataloader.CIFAR_FS import CIFAR_FS
from dataloader.FC100 import FC100
from dataloader.Places import Places
from dataloader.miniImageNet import MiniImageNet
from dataloader.samplers import CategoriesSampler
from metrics import compute_differential_entropy, compute_mutual_information, compute_precision, ROC_OOD
from models.mtl import MtlLearner
from utils.misc import pprint, count_acc, get_task_data
from utils.gpu_tools import set_gpu

def get_uncertainty_score(logits, label=0):
    assert label in [0, 1]

    diff_ents = compute_differential_entropy(logits).data.cpu()  # differential entropy
    mi = compute_mutual_information(logits).data.cpu()  # mutual information
    precs = compute_precision(logits).data.cpu()  # precision

    return diff_ents, mi, precs


def calculate_avg_std_ci95(data):
    avg = np.mean(np.array(data))
    std = np.std(np.array(data))
    ci95 = 1.96 * std / np.sqrt(len(data))

    return avg, std, ci95


parser = argparse.ArgumentParser()
# Basic parameters
parser.add_argument('--model_type', type=str, default='ResNet', choices=['ResNet'])  # The network architecture
parser.add_argument('--dataset', type=str, default='miniImageNet', choices=['miniImageNet', 'CIFAR-FS', 'FC100'])  # Dataset
parser.add_argument('--phase', type=str, default='meta_eval', choices=['pre_train', 'meta_train', 'meta_eval', 'OOD_test', 'threshold_test', 'active'])  # Phase
parser.add_argument('--seed', type=int, default=0)  # Manual seed for PyTorch, "0" means using random seed
parser.add_argument('--gpu', default='0')  # GPU id

# Parameters for meta-train phase
parser.add_argument('--max_epoch', type=int, default=100)  # Epoch number for meta-train phase
parser.add_argument('--train_num_batch', type=int, default=200)
parser.add_argument('--val_num_batch', type=int, default=600)
parser.add_argument('--way', type=int, default=5)  # Way number, how many classes in a task
parser.add_argument('--shot', type=int, default=1)  # Shot number, how many samples for one class in a task
parser.add_argument('--query', type=int, default=15)  # The number of training samples for each class in a task
parser.add_argument("--task_num", default=600, type=int, help="Number of test tasks")

parser.add_argument('--meta_lr1', type=float, default=0.0001)  # Learning rate for SS weights
parser.add_argument('--meta_lr2', type=float, default=0.001)  # Learning rate for FC weights
parser.add_argument('--meta_base_lr', type=float, default=0.01)  # Learning rate for the inner loop
parser.add_argument('--pre_base_lr', type=float, default=0.01)  # Learning rate for the inner loop
parser.add_argument('--update_step', type=int, default=200)  # The number of updates for the inner loop
parser.add_argument('--step_size', type=int, default=10)  # The number of epochs to reduce the meta learning rates
parser.add_argument('--gamma', type=float, default=0.5)  # Gamma for the meta-train learning rate decay
parser.add_argument('--loss_type', type=str, default='log', choices=['mse', 'log', 'digamma'])
parser.add_argument('--pretrain_evidence_weight', type=str, default='10')


# Set and print the parameters
args = parser.parse_args()
pprint(vars(args))
set_gpu(args.gpu)

print('==> Preparing In-distribution data...')
if args.dataset == 'miniImageNet':
    Dataset = MiniImageNet
elif args.dataset == 'CIFAR-FS':
    Dataset = CIFAR_FS
elif args.dataset == 'FC100':
    Dataset = FC100
# Load meta-train set
dataset = Dataset('test')
sampler = CategoriesSampler(dataset.label, args.task_num, args.way,
                            args.shot + args.query)
dataloader = DataLoader(dataset=dataset, batch_sampler=sampler, pin_memory=True)

print('==> Preparing Out-of-distribution data...')
OodDataset = Places
ood_dataset = OodDataset('test')
OodSampler = CategoriesSampler(ood_dataset.label, args.task_num, args.way,
                            args.shot + args.query)
ood_dataloader = DataLoader(dataset=ood_dataset, batch_sampler=OodSampler, pin_memory=True)

print('==> Preparing Model...')
model = MtlLearner(args)

# Load the meta-trained model
save_path1 = '_'.join([args.dataset, args.model_type, 'MT-EDL-{}'.format(args.pretrain_evidence_weight)])
save_path2 = 'loss' + str(args.loss_type) + '_lr1' + str(args.meta_lr1) + '_lr2' + str(args.meta_lr2) \
             + '_batch' + str(args.train_num_batch) + '_maxepoch' + str(args.max_epoch) + '_shot' + str(
    args.shot) + '_updatestep' + str(args.update_step)
args.save_path = '../MetaTEDL-logs/meta' + '/' + save_path1 + '_' + save_path2

# Load the meta-trained model
args.save_path = 'checkpoints/meta/{}-{}-shot/max_acc.pth'.format(args.dataset, args.shot)
print('==> Loading meta-training model from: ', args.save_path)
model.load_state_dict(torch.load(args.save_path)['params'])
model.to('cuda')
model.eval()

auroc_Dents = []
auroc_MIs = []
auroc_precisions = []


for task_id, (task, ood_task) in enumerate(zip(dataloader, ood_dataloader), 1):
    data_support, label_support, data_query, label_query = get_task_data(task, args)
    ood_data_support, ood_label_support, ood_data_query, ood_labels_query = get_task_data(ood_task, args)

    evidence, ood_evidence = model.ood_forward(data_support, label_support, data_query, ood_data_query)
    alpha = evidence + 1

    prob = alpha / torch.sum(alpha, dim=1, keepdim=True)

    log_alpha = torch.log(alpha)

    acc = count_acc(prob.detach(), label_query, logit=False)

    diff_ents, mi, precs = get_uncertainty_score(log_alpha, label=0)
    labels = torch.zeros_like(label_query).data.cpu()

    # evaluate on ood query set
    ood_alpha = ood_evidence + 1
    ood_log_alpha = torch.log(ood_alpha)

    ood_diff_ents, ood_mi, ood_precs = get_uncertainty_score(ood_log_alpha, label=1)
    ood_labels = torch.ones_like(ood_labels_query).data.cpu()

    # Concat the results
    all_diff_ents = torch.cat([diff_ents, ood_diff_ents])
    all_mis = torch.cat([mi, ood_mi])
    all_precs = torch.cat([precs, ood_precs])
    all_labels = torch.cat([labels, ood_labels])

    auroc_Dent, auroc_MI, auroc_precision = ROC_OOD(all_diff_ents, all_mis, all_precs, all_labels)
    auroc_Dents.append(auroc_Dent)
    auroc_MIs.append(auroc_MI)
    auroc_precisions.append(auroc_precision)

    avg_auroc_Dent, std_auroc_Dent, ci95_auroc_Dent = calculate_avg_std_ci95(auroc_Dents)
    avg_auroc_MI, std_auroc_MI, ci95_auroc_MI = calculate_avg_std_ci95(auroc_MIs)
    avg_auroc_precision, std_auroc_precision, ci95_auroc_precision = calculate_avg_std_ci95(auroc_precisions)

    print('Task [{}/{}]: AUROC_Dent: {:.1f} ± {:.1f} % ({:.1f} %), AUROC_MI: {:.1f} ± {:.1f} % ({:.1f} %), AUROC_precision: {:.1f} ± {:.1f} % ({:.1f} %)'.
        format(task_id, len(dataloader), avg_auroc_Dent, ci95_auroc_Dent, auroc_Dent, avg_auroc_MI, ci95_auroc_MI, auroc_MI, avg_auroc_precision,
               ci95_auroc_precision, auroc_precision))

    pass
