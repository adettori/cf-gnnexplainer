# Based on https://github.com/RexYing/gnn-model-explainer/blob/master/explainer/explain.py
import math
import time
import torch
import torch.nn as nn
import numpy as np
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
from utils.utils import get_degree_matrix
from .gcn_perturb_orig import GCNSyntheticPerturbOrig
from .gcn_perturb_delta import GCNSyntheticPerturbDelta
from .gcn_perturb_delta_CEM import GCNSyntheticPerturbCEM


class CFExplainer:
    """
    CF Explainer class, returns counterfactual subgraph
    """
    def __init__(self, model, cf_optimizer, lr, n_momentum, sub_adj, num_nodes, sub_feat,
                 n_hid, dropout, sub_label, num_classes, beta, task, cem_mode=None,
                 edge_del=False, edge_add=False, bernoulli=False, delta=False, rand_init=True,
                 history=False, device=None, verbose=False):

        super(CFExplainer, self).__init__()
        self.model = model
        self.cf_optimizer = cf_optimizer
        self.lr = lr
        self.n_momentum = n_momentum
        self.sub_adj = sub_adj
        self.num_nodes = num_nodes
        self.sub_feat = sub_feat
        self.n_hid = n_hid
        self.dropout = dropout
        self.sub_label = sub_label
        self.beta = beta
        self.task = task
        self.num_classes = num_classes
        self.cem_mode = cem_mode
        self.edge_del = edge_del
        self.edge_add = edge_add
        self.bernoulli = bernoulli
        self.delta = delta
        self.rand_init = rand_init
        self.history = history
        self.device = device
        self.verbose = verbose

        self.model.eval()

        if self.cem_mode is None and not edge_del and not edge_add:
            raise RuntimeError("CFExplainer: need to specify allowed add/del op")

        # Instantiate CF model class, load weights from original model
        if self.cem_mode == "PN" or self.cem_mode == "PP":
            self.cf_model = GCNSyntheticPerturbCEM(self.model, self.num_classes,
                                                   self.sub_adj, num_nodes, beta,
                                                   mode=self.cem_mode, rand_init=self.rand_init,
                                                   device=self.device, task=self.task)

        elif self.cem_mode is None:

            if self.delta:
                self.cf_model = GCNSyntheticPerturbDelta(self.model, self.num_classes,
                                                         self.sub_adj, num_nodes, self.beta,
                                                         edge_del=self.edge_del,
                                                         edge_add=self.edge_add,
                                                         bernoulli=self.bernoulli,
                                                         rand_init=self.rand_init,
                                                         device=self.device, task=self.task)
            else:
                self.cf_model = GCNSyntheticPerturbOrig(self.model, self.num_classes,
                                                        self.sub_adj, num_nodes, beta,
                                                        edge_del=self.edge_del,
                                                        edge_add=self.edge_add,
                                                        bernoulli=self.bernoulli,
                                                        rand_init=self.rand_init,
                                                        device=self.device, task=self.task)
        else:
            raise RuntimeError("cf_explainer: the specified mode for CEM is invalid")

        if self.verbose:
            for name, param in self.model.named_parameters():
                print("orig model requires_grad: ", name, param.requires_grad)
            for name, param in self.cf_model.named_parameters():
                print("cf model required_grad: ", name, param.requires_grad)

        # Init optimizer used to generate explanation
        if cf_optimizer == "SGD" and n_momentum == 0.0:
            self.cf_optimizer = optim.SGD(self.cf_model.parameters(), lr=lr)
        elif cf_optimizer == "SGD" and n_momentum != 0.0:
            self.cf_optimizer = optim.SGD(self.cf_model.parameters(), lr=lr, nesterov=True,
                                          momentum=n_momentum)
        elif cf_optimizer == "Adadelta":
            self.cf_optimizer = optim.Adadelta(self.cf_model.parameters(), lr=lr)


    def debug_check_expl(self, expl_example):

        # Check loss_graph_dist, handling edge case of PP which is not a CF
        if expl_example != [] and expl_example[-1] < 1 and self.cem_mode != "PP":
            error_str = "cf_explainer: loss_graph_dist cannot be smaller than 1. Check symmetry"
            raise RuntimeError(error_str)

        # Check cf_adj
        if expl_example != [] and 1 in torch.diagonal(expl_example[0], dim1=-2, dim2=-1):
            raise RuntimeError("cf_explainer: cf_adj contains a self-connection. Invalid result.")

        if expl_example != [] and torch.any(torch.greater(expl_example[0], 1)):
            raise RuntimeError("cf_explainer: cf_adj contains values > 1. Invalid result.")

        if expl_example != [] and torch.any(torch.less(expl_example[0], 0)):
            raise RuntimeError("cf_explainer: cf_adj contains values < 0. Invalid result.")


    def explain(self, task, num_epochs, y_pred_orig, node_idx=None, new_idx=None, debug=True):

        if task == "node-class" and (node_idx is None or new_idx is None):
            raise RuntimeError("cf_explainer/explain: invalid task")

        expl_list = []
        best_loss = np.inf
        num_expl = 0

        for epoch in range(num_epochs):

            if task == "node-class":
                new_expl, loss_total = self.train_expl(task, epoch, y_pred_orig,
                                                       node_idx, new_idx)
            elif task == "graph-class":
                new_expl, loss_total = self.train_expl(task, epoch, y_pred_orig)

            if self.verbose:
                print(loss_total, "(Current loss)")
                print(best_loss, "(Best loss)")

            # The best explanation is the last one
            if new_expl != [] and loss_total < best_loss:
                if self.history:
                    expl_list.append(new_expl)
                else:
                    expl_list = [new_expl]

                best_loss = loss_total
                num_expl += 1

            if debug:
                with torch.no_grad():
                    self.debug_check_expl(new_expl)

        expl_res = [node_idx, new_idx, expl_list, self.sub_adj.cpu(), self.sub_feat.cpu(),
                    self.sub_label.cpu(), y_pred_orig, self.num_nodes]

        return expl_res, num_expl


    def train_expl(self, task, epoch, y_pred_orig, node_idx=None, new_idx=None):
        self.cf_optimizer.zero_grad()

        output, output_actual = self.cf_model.forward(self.sub_feat)

        if task == "node-class":
            # Need to use new_idx from now on since sub_adj is reindexed
            output = output[new_idx]
            output_actual = output_actual[new_idx]

        y_pred_new = torch.argmax(output)
        y_pred_new_actual = torch.argmax(output_actual)

        # loss_pred indicator should be based on y_pred_new_actual NOT y_pred_new!
        if self.cem_mode == "PN":

            loss_total, loss_pred, loss_graph_dist, cf_adj = \
                self.cf_model.loss_PN(output, y_pred_orig, y_pred_new_actual)

        elif self.cem_mode == "PP":

            loss_total, loss_pred, loss_graph_dist, cf_adj = \
                self.cf_model.loss_PP(output, y_pred_orig, y_pred_new_actual)

        elif self.cem_mode is None:

            if self.bernoulli:
                loss_total, loss_pred, loss_graph_dist, cf_adj = \
                    self.cf_model.loss_bernoulli(output, y_pred_orig, y_pred_new_actual)
            else:
                loss_total, loss_pred, loss_graph_dist, cf_adj = \
                    self.cf_model.loss_std(output, y_pred_orig, y_pred_new_actual)
        else:
            raise RuntimeError("cf_explainer/train: the specified mode for CEM is invalid")

        loss_total.backward()
        clip_grad_norm_(self.cf_model.parameters(), 2.0)
        self.cf_optimizer.step()

        if self.verbose:
            print('Epoch: {:04d}'.format(epoch + 1),
                  'loss: {:.4f}'.format(loss_total.item()),
                  'pred loss: {:.4f}'.format(loss_pred.item()),
                  'graph loss: {:.4f}'.format(loss_graph_dist.item()),
                  'beta: {},'.format(self.beta))
            print('Output: {}\n'.format(output.data),
                  'Output nondiff: {}\n'.format(output_actual.data),
                  'orig pred: {}, '.format(y_pred_orig),
                  'new pred: {}, '.format(y_pred_new),
                  'new pred nondiff: {}'.format(y_pred_new_actual))
            print(" ")

        with torch.no_grad():
            # Note: when updating output format, also update checks
            expl_inst = []
            cond_PP = self.cem_mode == "PP" and y_pred_new_actual == y_pred_orig
            # Needed to avoid including PP with different predictions
            cond_cf = self.cem_mode != "PP" and y_pred_new_actual != y_pred_orig

            if cond_PP or cond_cf:
                expl_inst = [cf_adj.detach().squeeze().cpu(), y_pred_new_actual.cpu(),
                             loss_graph_dist.item()]

        return(expl_inst, loss_total.item())
