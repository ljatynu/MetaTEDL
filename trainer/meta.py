""" Trainer for meta-train phase. """
import os
import os.path as osp
import random

import numpy as np
import pandas as pd
import torch
import tqdm
from torch.utils.data import DataLoader

from dataloader.CIFAR_FS import CIFAR_FS
from dataloader.miniImageNet import MiniImageNet
from dataloader.FC100 import FC100
from dataloader.samplers import CategoriesSampler
from models.EDL_loss import edl_mse_loss, edl_log_loss, edl_digamma_loss
from models.mtl import MtlLearner
from utils.misc import Averager, Timer, count_acc, compute_confidence_interval, \
    ensure_path, ECELoss, get_entropy, plot_ECDF


def acc_with_threshold(preds, labels, uncertainty, threshold):
    under_threshold_index = uncertainty <= threshold
    preds_filter = preds[under_threshold_index]
    labels_filter = labels[under_threshold_index]
    filter_nums = len(labels_filter)
    if filter_nums == 0:
        return 0, 0
    else:
        match = torch.eq(preds_filter, labels_filter).float()
        acc_nums = torch.sum(match)
        return int(acc_nums), filter_nums


class MetaTrainer(object):
    """The class that contains the code for the meta-train phase and meta-eval phase."""

    def __init__(self, args):
        # Set the folder to save the records and checkpoints
        log_base_dir = 'logs/'
        if not osp.exists(log_base_dir):
            os.mkdir(log_base_dir)
        meta_base_dir = osp.join(log_base_dir, 'meta')
        if not osp.exists(meta_base_dir):
            os.mkdir(meta_base_dir)
        save_path1 = '_'.join([args.dataset, args.model_type, 'MT-EDL-1010'])
        save_path2 = 'loss' + str(args.loss_type) + '_lr1' + str(args.meta_lr1) + '_lr2' + str(args.meta_lr2) \
                     + '_batch' + str(args.train_num_batch) + '_maxepoch' + str(args.max_epoch) + '_shot' + str(
            args.shot) + '_updatestep' + str(args.update_step)
        args.save_path = meta_base_dir + '/' + save_path1 + '_' + save_path2
        ensure_path(args.save_path)

        # Set args to be shareable in the class
        self.args = args

        self.loss = edl_log_loss

        if self.args.dataset == 'miniImageNet':
            self.Dataset = MiniImageNet
        elif self.args.dataset == 'CIFAR-FS':
            self.Dataset = CIFAR_FS
        elif self.args.dataset == 'FC100':
            self.Dataset = FC100
        # Load meta-train set
        self.trainset = self.Dataset('train')
        self.train_sampler = CategoriesSampler(self.trainset.label, self.args.train_num_batch, self.args.way,
                                               self.args.shot + self.args.train_query)
        self.train_loader = DataLoader(dataset=self.trainset, batch_sampler=self.train_sampler, pin_memory=True)

        # Load meta-val set
        self.valset = self.Dataset('val')
        self.val_sampler = CategoriesSampler(self.valset.label, self.args.val_num_batch, self.args.way,
                                             self.args.shot + self.args.val_query)
        self.val_loader = DataLoader(dataset=self.valset, batch_sampler=self.val_sampler, pin_memory=True)
        self.model = MtlLearner(self.args)

        self.optimizer = torch.optim.Adam(
        [{'params': filter(lambda p: p.requires_grad, self.model.encoder.parameters()), 'lr': self.args.meta_lr1},
         {'params': self.model.meta_base_learner.parameters(), 'lr': self.args.meta_lr2},
         {'params': self.model.pre_base_learner.parameters(), 'lr': self.args.meta_lr2}])
        # Set learning rate scheduler
        self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=self.args.step_size,
                                                            gamma=self.args.gamma)

        # load pretrained model without FC classifier
        pre_train_path = osp.join('checkpoints/pre/' + self.args.dataset + '/max_acc.pth')
        print('Loading pretrain model from:', pre_train_path)
        pretrained_dict = torch.load(pre_train_path)['params']
        self.model_dict = self.model.state_dict()
        meta_pretrained_dict = {'encoder.' + k: v for k, v in pretrained_dict.items()}
        meta_pretrained_dict = {k: v for k, v in meta_pretrained_dict.items() if k in self.model_dict}
        pre_pretrained_dict = {'pre_encoder.' + k: v for k, v in pretrained_dict.items()}
        pre_pretrained_dict = {k: v for k, v in pre_pretrained_dict.items() if k in self.model_dict}
        self.model_dict.update(meta_pretrained_dict)
        self.model_dict.update(pre_pretrained_dict)
        self.model.load_state_dict(self.model_dict)

        # Set model to GPU
        if torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
            self.model = self.model.cuda()

    def save_model(self, name):
        torch.save(dict(params=self.model.state_dict()), osp.join(self.args.save_path, name + '.pth'))

    def meta_train(self):
        # Set the meta-train log
        trlog = {'args': vars(self.args), 'train_loss': [], 'val_loss': [], 'train_acc': [], 'val_acc': [],
                 'max_acc': 0.0, 'max_acc_epoch': 0}

        # Set the timer
        timer = Timer()

        label_support = torch.arange(self.args.way).repeat(self.args.shot)
        label_support = label_support.type(torch.cuda.LongTensor)

        # Generate the labels for test set of the episodes during meta-train updates
        label_query = torch.arange(self.args.way).repeat(self.args.train_query)
        label_query = label_query.type(torch.cuda.LongTensor)

        # Start meta-train
        for epoch in range(1, self.args.max_epoch + 1):
            # Update learning rate
            # Set the model to train mode
            self.model.train()
            # Set averager classes to record training losses and accuracies
            train_loss_averager = Averager()
            train_acc_averager = Averager()

            # # Using tqdm to read samples from train loader
            tqdm_gen = tqdm.tqdm(self.train_loader)
            for task_id, task in enumerate(tqdm_gen, 1):
                data = task[0].cuda()
                p = self.args.shot * self.args.way
                data_support, data_query = data[:p], data[p:]

                # Output logits for model
                evidence_q = self.model.meta_train_forward(data_support, label_support, data_query, epoch)
                alpha_q = evidence_q + 1
                prob_q = alpha_q / torch.sum(alpha_q, dim=1, keepdim=True)
                # Calculate meta-train loss
                loss = self.loss(alpha_q, label_query, epoch_num=epoch, num_classes=self.args.way,
                                 annealing_step=5 * self.args.max_epoch)
                # Calculate meta-train accuracy
                acc = count_acc(prob_q, label_query, logit=False)
                # Print loss and accuracy for this step

                # Add loss and accuracy for the averagers
                train_loss_averager.add(loss.item())
                train_acc_averager.add(acc)

                tqdm_gen.set_description('Epoch {}, Avg Train Loss={:.4f} Avg Train Acc={:.4f}'.format(epoch, train_loss_averager.data(), train_acc_averager.data()))

                # Loss backwards and optimizer updates
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

            # Update the averagers
            train_loss_averager = train_loss_averager.data()
            train_acc_averager = train_acc_averager.data()

            # Print previous information
            if epoch % 10 == 0:
                print('Best Epoch {}, Best Val Acc={:.4f}'.format(trlog['max_acc_epoch'], trlog['max_acc']))

            # Set averager classes to record validation losses and accuracies
            val_loss_averager = Averager()
            val_acc_averager = Averager()

            # Start validation for this epoch, set model to eval mode
            self.model.eval()
            # Run meta-validation
            tqdm_gen = tqdm.tqdm(self.val_loader)
            for task_id, task in enumerate(tqdm_gen, 1):
                data = task[0].cuda()
                p = self.args.shot * self.args.way
                data_support, data_query = data[:p], data[p:]

                evidence_q = self.model.meta_train_forward(data_support, label_support, data_query, epoch)
                alpha_q = evidence_q + 1
                prob_q = alpha_q / torch.sum(alpha_q, dim=1, keepdim=True)
                loss = self.loss(alpha_q, label_query, epoch_num=epoch, num_classes=self.args.way,
                                 annealing_step=5 * self.args.max_epoch)

                acc = count_acc(prob_q, label_query, logit=False)

                val_loss_averager.add(loss.item())
                val_acc_averager.add(acc)

                tqdm_gen.set_description('Epoch {}, Val, Avg Val Loss={:.4f} Avg Val Acc={:.4f}'.format(epoch, val_loss_averager.data(), val_acc_averager.data()))


            # Update validation averagers
            val_loss_averager = val_loss_averager.data()
            val_acc_averager = val_acc_averager.data()
            # Print loss and accuracy for this epoch
            print('Epoch {}, Val, Loss={:.4f} Acc={:.4f}'.format(epoch, val_loss_averager, val_acc_averager))

            # Update best saved model
            if val_acc_averager > trlog['max_acc']:
                trlog['max_acc'] = val_acc_averager
                trlog['max_acc_epoch'] = epoch
                self.save_model('max_acc')
            # Save model every 10 epochs
            if epoch % 20 == 0:
                self.save_model('epoch' + str(epoch))

            # self.save_model('epoch' + str(epoch))

            # Update the logs
            trlog['train_loss'].append(train_loss_averager)
            trlog['train_acc'].append(train_acc_averager)
            trlog['val_loss'].append(val_loss_averager)
            trlog['val_acc'].append(val_acc_averager)

            # Save log
            torch.save(trlog, osp.join(self.args.save_path, 'trlog'))
            self.lr_scheduler.step()
            if epoch % 10 == 0:
                print('Running Time: {}, Estimated Time: {}'.format(timer.measure(),
                                                                    timer.measure(epoch / self.args.max_epoch)))