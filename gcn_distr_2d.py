import os
import os.path as osp
import argparse

import math

import torch
import torch.distributed as dist

from torch_geometric.datasets import Planetoid, PPI, Reddit
from torch_geometric.nn import GCNConv, ChebConv  # noqa
from torch_geometric.utils import add_remaining_self_loops, to_dense_adj, dense_to_sparse, to_scipy_sparse_matrix
import torch_geometric.transforms as T

import torch.multiprocessing as mp

from torch.multiprocessing import Manager, Process

from torch.nn import Parameter
import torch.nn.functional as F

from torch_scatter import scatter_add

import socket
import time
import numpy as np

normalization = False

def normalize(adj_matrix):
    adj_matrix = adj_matrix + torch.eye(adj_matrix.size(0))
    d = torch.sum(adj_matrix, dim=1)
    d = torch.rsqrt(d)
    d = torch.diag(d)
    return torch.mm(d, torch.mm(adj_matrix, d))

def block_row(adj_matrix, am_partitions, inputs, weight, rank, size):
    n_per_proc = math.ceil(float(adj_matrix.size(1)) / size)
    # n_per_proc = int(adj_matrix.size(1) / size)
    # am_partitions = list(torch.split(adj_matrix, n_per_proc, dim=1))

    # z_loc = torch.cuda.FloatTensor(n_per_proc, inputs.size(1)).fill_(0)
    z_loc = torch.zeros(adj_matrix.size(0), inputs.size(1))
    
    inputs_recv = torch.zeros(inputs.size())

    part_id = rank % size

    z_loc += torch.mm(am_partitions[part_id].t(), inputs) 

    for i in range(1, size):
        part_id = (rank + i) % size

        inputs_recv = torch.zeros(am_partitions[part_id].size(0), inputs.size(1))

        src = (rank + 1) % size
        dst = rank - 1
        if dst < 0:
            dst = size - 1

        if rank == 0:
            dist.send(tensor=inputs, dst=dst)
            dist.recv(tensor=inputs_recv, src=src)
        else:
            dist.recv(tensor=inputs_recv, src=src)
            dist.send(tensor=inputs, dst=dst)
        
        inputs = inputs_recv.clone()

        # z_loc += torch.mm(am_partitions[part_id], inputs) 
        z_loc += torch.mm(am_partitions[part_id].t(), inputs) 


    # z_loc = torch.mm(z_loc, weight)
    return z_loc

def outer_product(adj_matrix, grad_output, rank, size, group):
    n_per_proc = math.ceil(float(adj_matrix.size(0)) / size)
    
    # A * G^l
    ag = torch.mm(adj_matrix, grad_output)

    # reduction on A * G^l low-rank matrices
    dist.all_reduce(ag, op=dist.reduce_op.SUM, group=group)

    # partition A * G^l by block rows and get block row for this process
    # TODO: this might not be space-efficient
    red_partitions = list(torch.split(ag, n_per_proc, dim=0))
    grad_input = red_partitions[rank]

    return grad_input

def outer_product2(inputs, ag, rank, size, group):
    # (H^(l-1))^T * (A * G^l)
    grad_weight = torch.mm(inputs, ag)
    
    # reduction on grad_weight low-rank matrices
    dist.all_reduce(grad_weight, op=dist.reduce_op.SUM, group=group)

    return grad_weight

def broad_func(adj_matrix, am_partitions, inputs, rank, size, group):
    n_per_proc = math.ceil(float(adj_matrix.size(1)) / size)

    # z_loc = torch.cuda.FloatTensor(adj_matrix.size(0), inputs.size(1)).fill_(0)
    z_loc = torch.zeros(adj_matrix.size(0), inputs.size(1))
    
    # inputs_recv = torch.cuda.FloatTensor(n_per_proc, inputs.size(1))
    inputs_recv = torch.zeros(n_per_proc, inputs.size(1))

    for i in range(size):
        if i == rank:
            inputs_recv = inputs.clone()
        elif i == size - 1:
            # inputs_recv = torch.cuda.FloatTensor(list(am_partitions[i].size())[1], inputs.size(1))
            inputs_recv = torch.zeros(list(am_partitions[i].t().size())[1], inputs.size(1))

        dist.broadcast(inputs_recv, src=i, group=group)

        # z_loc += torch.mm(am_partitions[i], inputs_recv) 
        z_loc += torch.mm(am_partitions[i].t(), inputs_recv) 

    return z_loc

def summa(adj_matrix, inputs, rank, row, col, size, row_groups, col_groups, transpose):
    proc_row = proc_row_size(size)
    proc_col = proc_col_size(size)

    acol = torch.FloatTensor(adj_matrix.size())

    brow = torch.FloatTensor(inputs.size())
    z_loc = torch.zeros(adj_matrix.size(0), inputs.size(1))

    for k in range(proc_col):

        if transpose:
            row_src_rank = k * proc_row + row
            col_src_rank = k + proc_row * col
        else:
            row_src_rank = k + proc_col * row
            col_src_rank = k * proc_col + col

        if row_src_rank == rank:
            acol = adj_matrix.clone()
        else:
            acol = torch.FloatTensor(adj_matrix.size())
        
        print("before bcast1: "+ str(transpose) + " " + str(row_src_rank) + " " + str(rank), flush=True)
        dist.broadcast(acol, row_src_rank, row_groups[row])
        print("after bcast1", flush=True)

        if col_src_rank == rank:
            brow = inputs.clone()
        else:
            brow = torch.FloatTensor(inputs.size())

        print("before bcast2: "+ str(transpose) + " " + str(col_src_rank) + " " + str(rank), flush=True)
        dist.broadcast(brow, col_src_rank, col_groups[col])
        print("after bcast2", flush=True)

        z_loc += torch.mm(acol, brow)

    return z_loc

def summa_sparse(adj_matrix, inputs, rank, row, col, size, row_groups, col_groups):
    proc_row = proc_row_size(size)
    proc_col = proc_col_size(size)

    acol = torch.sparse.FloatTensor(adj_matrix.size())

    brow = torch.FloatTensor(inputs.size())
    z_loc = torch.zeros(adj_matrix.size(0), inputs.size(1))

    for k in range(proc_col):

        for i in range(proc_row):
            if row == i and col == k:
                acol = adj_matrix.clone()
            else:
                acol = torch.sparse.FloatTensor(adj_matrix.size())
            acol = acol.coalesce()
            print("acol: " + str(acol))
            print("before bcast " + str(i), flush=True)
            dist.broadcast(acol.indices(), rank, row_groups[i])
            print("after bcast1 " + str(i), flush=True)
            dist.broadcast(acol.values(), rank, row_groups[i])
            print("after bcast2 " + str(i), flush=True)
            acol.size = adj_matrix.size()
            acol.nnz = len(acol.values())

        for j in range(proc_col):
            if row == k and col == j:
                brow = inputs.clone()
            else:
                brow = torch.FloatTensor(inputs.size())
            print("before bcast3 " + str(j), flush=True)
            dist.broadcast(brow, rank, col_groups[j])
            print("after bcast3 " + str(j), flush=True)

        z_loc += torch.mm(acol, brow)

    return z_loc

class GCNFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, inputs, weight, adj_matrix, am_partitions, rank, size, group, func):
        print("in forward", flush=True)
        # inputs: H
        # adj_matrix: A
        # weight: W
        # func: sigma

        adj_matrix = adj_matrix.to_dense()
        ctx.save_for_backward(inputs, weight, adj_matrix)
        ctx.rank = rank
        ctx.size = size
        ctx.group = group

        ctx.func = func

        proc_row = proc_row_size(size)
        proc_col = proc_col_size(size)
        
        rank_row = int(rank / proc_col)
        rank_col = rank % proc_col
            
        row_procs = []
        col_procs = []
        row_groups = []
        col_groups = []
        for i in range(proc_row):
            row_procs.append(list(range(i, size, proc_row)))

        for i in range(proc_col):
            col_procs.append(list(range(i * proc_row, i * proc_row + proc_row)))

        for i in range(len(row_procs)):
            row_groups.append(dist.new_group(row_procs[i]))

        for i in range(len(col_procs)):
            col_groups.append(dist.new_group(col_procs[i]))

        print("row_procs: " + str(row_procs))
        print("col_procs: " + str(col_procs))
        # rank_row and rank_col switch because transpose
        # z = summa_sparse(adj_matrix.t(), inputs, rank, rank_col, rank_row, size, row_groups, col_groups);
        z = summa(adj_matrix.t(), inputs, rank, rank_col, rank_row, size, row_groups, col_groups, True);

        z = torch.mm(z, weight)
        z.requires_grad = True
        ctx.z = z

        if func is F.log_softmax:
            h = func(z, dim=1)
        elif func is F.relu:
            h = func(z)
        else:
            h = z

        return h

    @staticmethod
    def backward(ctx, grad_output):
        inputs, weight, adj_matrix = ctx.saved_tensors
        rank = ctx.rank
        size = ctx.size
        group = ctx.group

        func = ctx.func
        z = ctx.z

        with torch.set_grad_enabled(True):
            if func is F.log_softmax:
                func_eval = func(z, dim=1)
            elif func is F.relu:
                func_eval = func(z)
            else:
                func_eval = z

            sigmap = torch.autograd.grad(outputs=func_eval, inputs=z, grad_outputs=grad_output)[0]
            grad_output = sigmap

        proc_row = proc_row_size(size)
        proc_col = proc_col_size(size)

        rank_row = int(rank / proc_col)
        rank_col = rank % proc_col
            
        row_procs = []
        col_procs = []
        row_groups = []
        col_groups = []
        for i in range(proc_row):
            row_procs.append(list(range(i * proc_col, i * proc_col + proc_col)) )

        for i in range(proc_col):
            col_procs.append(list(range(i, size, proc_col)))

        for i in range(len(row_procs)):
            row_groups.append(dist.new_group(row_procs[i]))

        for i in range(len(col_procs)):
            col_groups.append(dist.new_group(col_procs[i]))

        # First backprop equation
        # ag = summa_sparse(adj_matrix, grad_output, rank, rank_row, rank_col, size, row_groups, col_groups);
        ag = summa(adj_matrix, grad_output, rank, rank_row, rank_col, size, row_groups, col_groups, False);
        grad_input = torch.mm(ag, weight.t())

        # Second backprop equation (reuses the A * G^l computation)
        # rank_row/groups and rank_col/groups switch because transpose
        print("before dense", flush=True)
        grad_weight = summa(inputs.t(), ag, rank, rank_col, rank_row, size, col_groups, row_groups, True);
        print("after dense", flush=True)

        return grad_input, grad_weight, None, None, None, None, None, None

def train(inputs, weight1, weight2, adj_matrix, am_partitions, optimizer, data, rank, size, group):
    outputs = GCNFunc.apply(inputs, weight1, adj_matrix, am_partitions, rank, size, group, F.relu)
    outputs = GCNFunc.apply(outputs, weight2, adj_matrix, am_partitions, rank, size, group, F.log_softmax)

    optimizer.zero_grad()
    rank_train_mask = torch.split(data.train_mask.bool(), outputs.size(0), dim=0)[rank]
    datay_rank = torch.split(data.y, outputs.size(0), dim=0)[rank]

    # Note: bool type removes warnings, unsure of perf penalty
    # loss = F.nll_loss(outputs[data.train_mask.bool()], data.y[data.train_mask.bool()])
    if list(datay_rank[rank_train_mask].size())[0] > 0:
    # if datay_rank.size(0) > 0:
        loss = F.nll_loss(outputs[rank_train_mask], datay_rank[rank_train_mask])
        # loss = F.nll_loss(outputs, torch.max(datay_rank, 1)[1])
        loss.backward()
    else:
        # fake_loss = (outputs * torch.cuda.FloatTensor(outputs.size()).fill_(0)).sum()
        fake_loss = (outputs * torch.zeros(outputs.size())).sum()
        fake_loss.backward()

    optimizer.step()

    return outputs

def test(outputs, data, vertex_count, rank):
    logits, accs = outputs, []
    datay_rank = torch.split(data.y, vertex_count)[rank]
    for _, mask in data('train_mask', 'val_mask', 'test_mask'):
        mask_rank = torch.split(mask, vertex_count)[rank]
        count = mask_rank.nonzero().size(0)
        if count > 0:
            pred = logits[mask_rank].max(1)[1]
            acc = pred.eq(datay_rank[mask_rank]).sum().item() / mask_rank.sum().item()
            # pred = logits[mask].max(1)[1]
            # acc = pred.eq(data.y[mask]).sum().item() / mask.sum().item()
        else:
            acc = -1
        accs.append(acc)
    return accs


# Split a COO into partitions of size n_per_proc
# Basically torch.split but for Sparse Tensors since pytorch doesn't support that.
def split_coo(adj_matrix, node_count, n_per_proc, dim):
    vtx_indices = list(range(0, node_count, n_per_proc))
    vtx_indices.append(node_count)

    am_partitions = []
    for i in range(len(vtx_indices) - 1):
        am_part = adj_matrix[:,(adj_matrix[dim,:] >= vtx_indices[i]).nonzero().squeeze(1)]
        am_part = am_part[:,(am_part[dim,:] < vtx_indices[i + 1]).nonzero().squeeze(1)]
        am_part[dim] -= vtx_indices[i]
        am_partitions.append(am_part)

    return am_partitions, vtx_indices

# Normalize all elements according to KW's normalization rule
def scale_elements(adj_matrix, adj_part, node_count, row_vtx, col_vtx):
    if not normalization:
        return

    # Scale each edge (u, v) by 1 / (sqrt(u) * sqrt(v))
    indices = adj_part._indices()
    values = adj_part._values()

    deg_map = dict()
    for i in range(adj_part._nnz()):
        u = indices[0][i] + row_vtx
        v = indices[1][i] + col_vtx

        if u.item() in deg_map:
            degu = deg_map[u.item()]
        else:
            degu = (adj_matrix[0] == u).sum().item()
            deg_map[u.item()] = degu

        if v.item() in deg_map:
            degv = deg_map[v.item()]
        else:
            degv = (adj_matrix[0] == v).sum().item()
            deg_map[v.item()] = degv

        values[i] = values[i] / (math.sqrt(degu) * math.sqrt(degv))
    
    # deg = torch.histc(adj_matrix[0].float(), bins=node_count)
    # deg = deg.pow(-0.5)

    # row_len = adj_part.size(0)
    # col_len = adj_part.size(1)

    # dleft = torch.sparse_coo_tensor([np.arange(row_vtx, row_vtx + row_len).tolist(),
    #                                  np.arange(row_vtx, row_vtx + row_len).tolist()],
    #                                  deg[row_vtx:(row_vtx + row_len)],
    #                                  size=(row_len, row_len),
    #                                  requires_grad=False)

    # dright = torch.sparse_coo_tensor([np.arange(col_vtx, col_vtx + col_len).tolist(),
    #                                  np.arange(col_vtx, col_vtx + col_len).tolist()],
    #                                  deg[row_vtx:(col_vtx + col_len)],
    #                                  size=(col_len, col_len),
    #                                  requires_grad=False)

    # adj_part = torch.sparse.mm(torch.sparse.mm(dleft, adj_part), dright)
    # return adj_part

def oned_partition(rank, size, inputs, adj_matrix, data, features, classes, device):
    node_count = inputs.size(0)
    n_per_proc = math.ceil(float(node_count) / size)

    am_partitions = None
    am_pbyp = None

    # Compute the adj_matrix and inputs partitions for this process
    # TODO: Maybe I do want grad here. Unsure.
    with torch.no_grad():
        # Column partitions
        am_partitions, vtx_indices = split_coo(adj_matrix, node_count, n_per_proc, 1)

        proc_node_count = vtx_indices[rank + 1] - vtx_indices[rank]
        am_pbyp, _ = split_coo(am_partitions[rank], node_count, n_per_proc, 0)
        for i in range(len(am_pbyp)):
            if i == size - 1:
                last_node_count = vtx_indices[i + 1] - vtx_indices[i]
                am_pbyp[i] = torch.sparse_coo_tensor(am_pbyp[i], torch.ones(am_pbyp[i].size(1)), 
                                                        size=(last_node_count, proc_node_count),
                                                        requires_grad=False)

                scale_elements(adj_matrix, am_pbyp[i], node_count, vtx_indices[i], vtx_indices[rank])
            else:
                am_pbyp[i] = torch.sparse_coo_tensor(am_pbyp[i], torch.ones(am_pbyp[i].size(1)), 
                                                        size=(n_per_proc, proc_node_count),
                                                        requires_grad=False)

                scale_elements(adj_matrix, am_pbyp[i], node_count, vtx_indices[i], vtx_indices[rank])

        for i in range(len(am_partitions)):
            proc_node_count = vtx_indices[i + 1] - vtx_indices[i]
            am_partitions[i] = torch.sparse_coo_tensor(am_partitions[i], torch.ones(am_partitions[i].size(1)), 
                                                    size=(node_count, proc_node_count), 
                                                    requires_grad=False)
            scale_elements(adj_matrix, am_partitions[i], node_count,  0, vtx_indices[i])

        input_partitions = torch.split(inputs, math.ceil(float(inputs.size(0)) / size), dim=0)

        adj_matrix_loc = am_partitions[rank]
        inputs_loc = input_partitions[rank]

    return inputs_loc, adj_matrix_loc, am_pbyp

def proc_row_size(size):
    return math.floor(math.sqrt(size))

def proc_col_size(size):
    return math.floor(math.sqrt(size))

def twod_partition(rank, size, inputs, adj_matrix, data, features, classes, device):
    node_count = inputs.size(0)
    proc_row = proc_row_size(size)
    proc_col = proc_col_size(size)

    n_per_proc = math.ceil(float(node_count) / proc_row)

    rank_row = int(rank / proc_col)
    rank_col = rank % proc_col
    
    print("row: " + str(rank_row), flush=True)
    print("col: " + str(rank_col), flush=True)

    am_partitions = None
    am_pbyp = None

    # Compute the adj_matrix and inputs partitions for this process
    # TODO: Maybe I do want grad here. Unsure.
    with torch.no_grad():
        # Column partitions
        am_partitions, vtx_indices = split_coo(adj_matrix, node_count, n_per_proc, 1)

        proc_node_count = vtx_indices[rank_col + 1] - vtx_indices[rank_col]
        am_pbyp, _ = split_coo(am_partitions[rank_col], node_count, n_per_proc, 0)
        for i in range(len(am_pbyp)):
            if i == size - 1:
                last_node_count = vtx_indices[i + 1] - vtx_indices[i]
                am_pbyp[i] = torch.sparse_coo_tensor(am_pbyp[i], torch.ones(am_pbyp[i].size(1)), 
                                                        size=(last_node_count, proc_node_count),
                                                        requires_grad=False)

                scale_elements(adj_matrix, am_pbyp[i], node_count, vtx_indices[i], vtx_indices[rank_col])
            else:
                am_pbyp[i] = torch.sparse_coo_tensor(am_pbyp[i], torch.ones(am_pbyp[i].size(1)), 
                                                        size=(n_per_proc, proc_node_count),
                                                        requires_grad=False)

                scale_elements(adj_matrix, am_pbyp[i], node_count, vtx_indices[i], vtx_indices[rank_col])

        for i in range(len(am_partitions)):
            proc_node_count = vtx_indices[i + 1] - vtx_indices[i]
            am_partitions[i] = torch.sparse_coo_tensor(am_partitions[i], torch.ones(am_partitions[i].size(1)), 
                                                    size=(node_count, proc_node_count), 
                                                    requires_grad=False)
            scale_elements(adj_matrix, am_partitions[i], node_count,  0, vtx_indices[i])

        input_rowparts = torch.split(inputs, math.ceil(float(inputs.size(0)) / proc_row), dim=0)
        input_partitions = []
        for i in input_rowparts:
            input_partitions.append(torch.split(i, math.ceil(float(inputs.size(1)) / proc_col), dim=1))

        adj_matrix_loc = am_pbyp[rank_row]
        inputs_loc = input_partitions[rank_row][rank_col]

    print(adj_matrix_loc.size(), flush=True)
    print(inputs_loc.size(), flush=True)
    return inputs_loc, adj_matrix_loc, am_pbyp

def run(rank, size, inputs, adj_matrix, data, features, classes, device):
    best_val_acc = test_acc = 0
    outputs = None
    group = dist.new_group(list(range(size)))

    # adj_matrix_loc = torch.rand(node_count, n_per_proc)
    # inputs_loc = torch.rand(n_per_proc, inputs.size(1))

    torch.manual_seed(0)
    weight1_nonleaf = torch.rand(features, 16, requires_grad=True)
    weight1_nonleaf = weight1_nonleaf.to(device)
    weight1_nonleaf.retain_grad()

    weight2_nonleaf = torch.rand(16, classes, requires_grad=True)
    weight2_nonleaf = weight2_nonleaf.to(device)
    weight2_nonleaf.retain_grad()

    weight1 = Parameter(weight1_nonleaf)
    weight2 = Parameter(weight2_nonleaf)

    optimizer = torch.optim.Adam([weight1, weight2], lr=0.01)

    print("before partition", flush=True)
    inputs_loc, adj_matrix_loc, am_pbyp = twod_partition(rank, size, inputs, adj_matrix, data, features, 
                                                                classes, device)
    print("after partition", flush=True)

    dist.barrier(group)
    tstart = 0.0
    tstop = 0.0
    if rank == 0:
        tstart = time.time()

    for epoch in range(1, 201):
    # for epoch in range(2):
        outputs = train(inputs_loc, weight1, weight2, adj_matrix_loc, am_pbyp, optimizer, data, 
                                rank, size, group)
        print("Epoch: {:03d}".format(epoch), flush=True)

    dist.barrier(group)
    if rank == 0:
        tstop = time.time()

    print("Time: " + str(tstop - tstart))
    
    # All-gather outputs to test accuracy
    # output_parts = []
    # for i in range(size):
    #     output_parts.append(torch.cuda.FloatTensor(am_partitions[0].size(1), classes).fill_(0))

    # dist.all_gather(output_parts, outputs)
    # outputs = torch.cat(output_parts, dim=0)

    # train_acc, val_acc, tmp_test_acc = test(outputs, data, am_partitions[0].size(1), rank)
    # if val_acc > best_val_acc:
    #     best_val_acc = val_acc
    #     test_acc = tmp_test_acc
    # log = 'Epoch: {:03d}, Train: {:.4f}, Val: {:.4f}, Test: {:.4f}'

    # print(log.format(200, train_acc, best_val_acc, test_acc))
    print("rank: " + str(rank) + " " +  str(outputs))
    return outputs

def init_process(rank, size, inputs, adj_matrix, data, features, classes, device, outputs, fn):
    run_outputs = fn(rank, size, inputs, adj_matrix, data, features, classes, device)
    if outputs is not None:
        outputs[rank] = run_outputs.detach()

def main(P, correctness_check):
    print(socket.gethostname())
    dataset = 'Cora'
    path = osp.join(osp.dirname(osp.realpath(__file__)), '..', 'data', dataset)
    dataset = Planetoid(path, dataset, T.NormalizeFeatures())
    # dataset = PPI(path, 'train', T.NormalizeFeatures())
    # dataset = Reddit(path, T.NormalizeFeatures())
    data = dataset[0]

    seed = 0

    mp.set_start_method('spawn', force=True)
    device = torch.device('cpu')
    # device = torch.device('cuda')

    data = data.to(device)
    data.x.requires_grad = True
    inputs = data.x.to(device)
    inputs.requires_grad = True
    data.y = data.y.to(device)

    edge_index = data.edge_index

    if normalization:
        adj_matrix, _ = add_remaining_self_loops(edge_index)
    else:
        adj_matrix = edge_index

    outputs = None
    dist.init_process_group(backend='mpi')
    rank = dist.get_rank()
    size = dist.get_world_size()
    print("Processes: " + str(size))

    init_process(rank, size, inputs, adj_matrix, data, dataset.num_features, dataset.num_classes, device, 
                        outputs, run)

    if outputs is not None:
        return outputs[0]

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--use_gdc', action='store_true',
                        help='Use GDC preprocessing.')
    parser.add_argument('--processes', metavar='P', type=int,
                        help='Number of processes')
    parser.add_argument('--correctness', metavar='C', type=str,
                        help='Run correctness check')
    args = parser.parse_args()
    print(args)
    P = args.processes
    correctness_check = args.correctness
    if P is None:
        P = 1

    if correctness_check is None or correctness_check == "nocheck":
        correctness_check = False
    else:
        correctness_check = True
    
    print("Correctness: " + str(correctness_check))
    print(main(P, correctness_check))
