import os
import errno
import torch
import numpy as np
import pandas as pd
from torch_geometric.utils import k_hop_subgraph, dense_to_sparse, to_dense_adj, subgraph


# Used to implement Bernoulli rv approach to P generation in Srinivas paper
class BernoulliMLSample(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input):

        ctx.save_for_backward(input)

        # ML sampling
        return (input >= 0.5).float()

    @staticmethod
    def backward(ctx, grad_output):
        # Pass-through estimator of bernoulli
        return grad_output

def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def safe_open(path, w):
    ''' Open "path" for writing, creating any parent directories as needed.'''
    mkdir_p(os.path.dirname(path))
    return open(path, w)


def get_degree_matrix(batch_adj):
    # Output: vector containing the sum for each row
    return torch.diag_embed(torch.sum(batch_adj, -1))


def normalize_adj(batch_adj, norm_eye=None):
    # Normalize adjacancy matrix according to reparam trick in GCN paper
    if norm_eye is None:
        A_tilde = batch_adj + torch.eye(batch_adj.shape[1], device=batch_adj.device)
    else:
        A_tilde = batch_adj + norm_eye
    D_tilde = get_degree_matrix(A_tilde).detach()  # Don't need gradient
    # Raise to power -1/2, set all infs to 0s
    D_tilde_exp = D_tilde ** (-1 / 2)
    D_tilde_exp[torch.isinf(D_tilde_exp)] = 0

    # Create norm_adj = (D + I)^(-1/2) * (A + I) * (D + I)^(-1/2)
    norm_batch_adj = torch.matmul(torch.matmul(D_tilde_exp, A_tilde), D_tilde_exp)

    return norm_batch_adj

def get_neighbourhood(node_idx, edge_index, n_hops, features, labels):
    # Get all nodes involved and relabel them
    edge_subset = k_hop_subgraph(node_idx, n_hops, edge_index[0], relabel_nodes=True)
    sub_adj = to_dense_adj(edge_subset[1]).squeeze()
    sub_feat = features[edge_subset[0], :]
    sub_labels = labels[edge_subset[0]]
    new_index = np.array(range(len(edge_subset[0])))
    # Maps orig labels to new
    node_dict = {edge_subset[0][i].item(): new_index[i] for i in range(len(edge_subset[0]))}
    # print("Num nodes in subgraph: {}".format(len(edge_subset[0])))
    return sub_adj, sub_feat, sub_labels, node_dict


# Create a symmetric matrix starting from the lower triangular part of another one
# The code is designed to avoid allocating additional tensors
# Note: ignores diagonal, assumes square matrix input
def create_symm_matrix_tril(matrix, final_side_len):
    orig_side_len = matrix.shape[0]

    symm_matrix = torch.tril(matrix, -1) + torch.tril(matrix, -1).t()

    # The symmetric matrix needs to be padded
    if final_side_len != orig_side_len:
        nodes_diff = abs(final_side_len - orig_side_len)

        # Pad bottom and right
        pad_f = torch.nn.ZeroPad2d((0, nodes_diff, 0, nodes_diff))
        symm_matrix = pad_f(symm_matrix)

    return symm_matrix
