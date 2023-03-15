# Copyright (c) 2022, NVIDIA CORPORATION.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import os
import time
from optparse import OptionParser

import apex
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.cuda.amp import autocast, GradScaler
from apex.parallel import DistributedDataParallel as DDP
from mpi4py import MPI
from torch.utils.data import DataLoader
from wg_torch import comm as comm
from wg_torch import embedding_ops as embedding_ops
from wg_torch import graph_ops as graph_ops
from wg_torch.wm_tensor import *

from wholegraph.torch import wholegraph_pytorch as wg

parser = OptionParser()
parser.add_option(
    "-r",
    "--root_dir",
    dest="root_dir",
    default="/nvme/songxiaoniu/graph-learning/wholegraph",
    # default="/dev/shm/dataset",
    help="dataset root directory.",
)
parser.add_option(
    "-g",
    "--graph_name",
    dest="graph_name",
    default="ogbn-papers100M",
    # default="papers100M",
    help="graph name",
)
parser.add_option(
    "-e", "--epochs", type="int", dest="epochs", default=4, help="number of epochs"
)
parser.add_option(
    "-b", "--batchsize", type="int", dest="batchsize", default=8000, help="batch size"
)
parser.add_option("--skip_epoch", type="int", dest="skip_epoch", default=2, help="num of skip epoch for profile")
parser.add_option("--local_step", type="int", dest="local_step", default=19, help="num of steps on a GPU in an epoch")
parser.add_option(
    "-n",
    "--neighbors",
    dest="neighbors",
    # default="5,5",
    default="10,25",
    help="train neighboor sample count",
)
parser.add_option(
    "--hiddensize", type="int", dest="hiddensize", default=256, help="hidden size"
)
parser.add_option(
    "-l", "--layernum", type="int", dest="layernum", default=2, help="layer number"
)
parser.add_option(
    "-m",
    "--model",
    dest="model",
    default="sage",
    help="model type, valid values are: sage, gcn, gat",
)
parser.add_option(
    "-f",
    "--framework",
    dest="framework",
    default="dgl",
    help="framework type, valid values are: dgl, pyg, wg",
)
parser.add_option("--heads", type="int", dest="heads", default=1, help="num heads")
parser.add_option(
    "-s",
    "--inferencesample",
    type="int",
    dest="inferencesample",
    default=30,
    help="inference sample count, -1 is all",
)
parser.add_option(
    "-w",
    "--dataloaderworkers",
    type="int",
    dest="dataloaderworkers",
    default=0,
    help="number of workers for dataloader",
)
parser.add_option(
    "-d", "--dropout", type="float", dest="dropout", default=0.5, help="dropout"
)
parser.add_option("--lr", type="float", dest="lr", default=0.003, help="learning rate")
parser.add_option(
    "--use_nccl",
    action="store_true",
    dest="use_nccl",
    default=False,
    help="whether use nccl for embeddings, default False",
)
parser.add_option(
    "--amp-off",
    action="store_false",
    dest="use_amp",
    default=True,
    help="whether use amp for training, default True",
)

(options, args) = parser.parse_args()


def parse_max_neighbors(num_layer, neighbor_str):
    neighbor_str_vec = neighbor_str.split(",")
    max_neighbors = []
    for ns in neighbor_str_vec:
        max_neighbors.append(int(ns))
    assert len(max_neighbors) == 1 or len(max_neighbors) == num_layer
    if len(max_neighbors) != num_layer:
        for i in range(1, num_layer):
            max_neighbors.append(max_neighbors[0])
    # max_neighbors.reverse()
    return max_neighbors


if options.framework == "dgl":
    import dgl
    from dgl.nn.pytorch.conv import SAGEConv, GATConv
elif options.framework == "pyg":
    from torch_sparse import SparseTensor
    from torch_geometric.nn import SAGEConv, GATConv
elif options.framework == "wg":
    from wg_torch.gnn.SAGEConv import SAGEConv
    from wg_torch.gnn.GATConv import GATConv


def get_train_step(sample_count, epochs, batch_size, global_size):
    return sample_count * epochs // (batch_size * global_size)


def create_test_dataset(data_tensor_dict):
    return DataLoader(
        dataset=graph_ops.NodeClassificationDataset(data_tensor_dict, 0, 1),
        batch_size=(options.batchsize + 3) // 4,
        shuffle=False,
        pin_memory=True,
    )


def create_gnn_layers(in_feat_dim, hidden_feat_dim, class_count, num_layer, num_head):
    gnn_layers = torch.nn.ModuleList()
    for i in range(num_layer):
        layer_output_dim = (
            hidden_feat_dim // num_head if i != num_layer - 1 else class_count
        )
        layer_input_dim = in_feat_dim if i == 0 else hidden_feat_dim
        mean_output = True if i == num_layer - 1 else False
        if options.framework == "pyg":
            if options.model == "sage":
                gnn_layers.append(SAGEConv(layer_input_dim, layer_output_dim))
            elif options.model == "gat":
                concat = not mean_output
                gnn_layers.append(
                    GATConv(
                        layer_input_dim, layer_output_dim, heads=num_head, concat=concat
                    )
                )
            else:
                assert options.model == "gcn"
                gnn_layers.append(
                    SAGEConv(layer_input_dim, layer_output_dim, root_weight=False)
                )
        elif options.framework == "dgl":
            if options.model == "sage":
                gnn_layers.append(SAGEConv(layer_input_dim, layer_output_dim, "mean"))
            elif options.model == "gat":
                gnn_layers.append(
                    GATConv(
                        layer_input_dim,
                        layer_output_dim,
                        num_heads=num_head,
                        allow_zero_in_degree=True,
                    )
                )
            else:
                assert options.model == "gcn"
                gnn_layers.append(SAGEConv(layer_input_dim, layer_output_dim, "gcn"))
        elif options.framework == "wg":
            if options.model == "sage":
                gnn_layers.append(SAGEConv(layer_input_dim, layer_output_dim))
            elif options.model == "gat":
                gnn_layers.append(
                    GATConv(
                        layer_input_dim,
                        layer_output_dim,
                        num_heads=num_head,
                        mean_output=mean_output,
                    )
                )
            else:
                assert options.model == "gcn"
                gnn_layers.append(
                    SAGEConv(layer_input_dim, layer_output_dim, aggregator="gcn")
                )
    return gnn_layers


def create_sub_graph(
    target_gid,
    target_gid_1,
    edge_data,
    csr_row_ptr,
    csr_col_ind,
    sample_dup_count,
    add_self_loop: bool,
):
    if options.framework == "pyg":
        neighboor_dst_unique_ids = csr_col_ind
        neighboor_src_unique_ids = edge_data[1]
        target_neighbor_count = target_gid.size()[0]
        if add_self_loop:
            self_loop_ids = torch.arange(
                0,
                target_gid_1.size()[0],
                dtype=neighboor_dst_unique_ids.dtype,
                device=target_gid.device,
            )
            edge_index = SparseTensor(
                row=torch.cat([neighboor_src_unique_ids, self_loop_ids]).long(),
                col=torch.cat([neighboor_dst_unique_ids, self_loop_ids]).long(),
                sparse_sizes=(target_gid_1.size()[0], target_neighbor_count),
            )
        else:
            edge_index = SparseTensor(
                row=neighboor_src_unique_ids.long(),
                col=neighboor_dst_unique_ids.long(),
                sparse_sizes=(target_gid_1.size()[0], target_neighbor_count),
            )
        return edge_index
    elif options.framework == "dgl":
        if add_self_loop:
            self_loop_ids = torch.arange(
                0,
                target_gid_1.numel(),
                dtype=edge_data[0].dtype,
                device=target_gid.device,
            )
            block = dgl.create_block(
                (
                    torch.cat([edge_data[0], self_loop_ids]),
                    torch.cat([edge_data[1], self_loop_ids]),
                ),
                num_src_nodes=target_gid.size(0),
                num_dst_nodes=target_gid_1.size(0),
            )
        else:
            block = dgl.create_block(
                (edge_data[0], edge_data[1]),
                num_src_nodes=target_gid.size(0),
                num_dst_nodes=target_gid_1.size(0),
            )
        return block
    else:
        assert options.framework == "wg"
        return [csr_row_ptr, csr_col_ind, sample_dup_count]
    return None


def layer_forward(layer, x_feat, x_target_feat, sub_graph):
    if options.framework == "pyg":
        x_feat = layer((x_feat, x_target_feat), sub_graph)
    elif options.framework == "dgl":
        x_feat = layer(sub_graph, (x_feat, x_target_feat))
    elif options.framework == "wg":
        x_feat = layer(sub_graph[0], sub_graph[1], sub_graph[2], x_feat, x_target_feat)
    return x_feat


class HomoGNNModel(torch.nn.Module):
    def __init__(
        self,
        graph: graph_ops.HomoGraph,
        num_layer,
        hidden_feat_dim,
        class_count,
        max_neighbors: str,
    ):
        super().__init__()
        self.graph = graph
        self.num_layer = num_layer
        self.hidden_feat_dim = hidden_feat_dim
        self.max_neighbors = parse_max_neighbors(num_layer, max_neighbors)
        self.class_count = class_count
        num_head = options.heads if (options.model == "gat") else 1
        assert hidden_feat_dim % num_head == 0
        in_feat_dim = self.graph.node_feat_shape()[1]
        self.gnn_layers = create_gnn_layers(
            in_feat_dim, hidden_feat_dim, class_count, num_layer, num_head
        )
        self.mean_output = True if options.model == "gat" else False
        self.add_self_loop = True if options.model == "gat" else False
        self.gather_fn = embedding_ops.EmbeddingLookUpModule(need_backward=False)
        self.dropout = nn.Dropout(options.dropout)

    def forward(self, ids):
        torch.cuda.synchronize()
        step_start_time = time.time()
        ids = ids.to(self.graph.id_type()).cuda()
        (
            target_gids,
            edge_indice,
            csr_row_ptrs,
            csr_col_inds,
            sample_dup_counts,
        ) = self.graph.unweighted_sample_without_replacement(ids, self.max_neighbors)
        torch.cuda.synchronize()
        sample_end_time = time.time()
        x_feat = self.gather_fn(target_gids[0], self.graph.node_feat)
        torch.cuda.synchronize()
        extract_end_time = time.time()
        # x_feat = self.graph.gather(target_gids[0])
        for i in range(self.num_layer):
            x_target_feat = x_feat[: target_gids[i + 1].numel()]
            sub_graph = create_sub_graph(
                target_gids[i],
                target_gids[i + 1],
                edge_indice[i],
                csr_row_ptrs[i],
                csr_col_inds[i],
                sample_dup_counts[i],
                self.add_self_loop,
            )
            x_feat = layer_forward(self.gnn_layers[i], x_feat, x_target_feat, sub_graph)
            if i != self.num_layer - 1:
                if options.framework == "dgl":
                    x_feat = x_feat.flatten(1)
                x_feat = F.relu(x_feat)
                x_feat = self.dropout(x_feat)
        if options.framework == "dgl" and self.mean_output:
            out_feat = x_feat.mean(1)
        else:
            out_feat = x_feat
        latency_s = (sample_end_time - step_start_time)
        latency_e = (extract_end_time - sample_end_time)
        return out_feat, (latency_s, latency_e, extract_end_time)


def valid_test(dataloader, model, name):
    total_correct = 0
    total_valid_sample = 0
    if comm.get_rank() == 0:
        print("%s..." % (name,))
    for i, (idx, label) in enumerate(dataloader):
        label = torch.reshape(label, (-1,)).cuda()
        model.eval()
        logits, _ = model(idx)
        pred = torch.argmax(logits, 1)
        correct = (pred == label).sum()
        total_correct += correct.cpu()
        total_valid_sample += label.shape[0]
    if comm.get_rank() == 0:
        print(
            "[%s] [%s] accuracy=%5.2f%%"
            % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                name,
                100.0 * total_correct / total_valid_sample,
            )
        )


def valid(valid_dataloader, model):
    valid_test(valid_dataloader, model, "VALID")


def test(test_data, model):
    test_dataloader = create_test_dataset(data_tensor_dict=test_data)
    valid_test(test_dataloader, model, "TEST")

def create_train_dataset(data_tensor_dict, rank, size):
    return DataLoader(
        dataset=graph_ops.NodeClassificationDataset(data_tensor_dict, rank, size),
        batch_size=options.batchsize,
        shuffle=True,
        num_workers=options.dataloaderworkers,
        pin_memory=True,
    )


def create_valid_dataset(data_tensor_dict):
    return DataLoader(
        dataset=graph_ops.NodeClassificationDataset(data_tensor_dict, 0, 1),
        batch_size=(options.batchsize + 3) // 4,
        shuffle=False,
        pin_memory=True,
    )


def train(train_data, valid_data, model, optimizer):
    if comm.get_rank() == 0:
        print("start training...")
    train_dataloader = create_train_dataset(
        data_tensor_dict=train_data, rank=comm.get_rank(), size=comm.get_world_size()
    )
    valid_dataloader = create_valid_dataset(data_tensor_dict=valid_data)
    total_steps = options.epochs* options.local_step
    profile_steps = (options.epochs - options.skip_epoch) * options.local_step
    if comm.get_rank() == 0:
        print(
            "epoch=%d total_steps=%d"
            % (
                options.epochs,
                total_steps,
            )
        )
    loss_fcn = torch.nn.CrossEntropyLoss()
    scaler = GradScaler()
    model.train()

    # directly enumerate train_dataloader
    test_start_time = time.time()
    cnt = 0
    for i, (idx, label) in enumerate(train_dataloader):
        cnt += 1
    test_end_time = time.time()
    print(
        "!!!!Train_dataloader(with %d items) enumerate latency: %f"
        % (cnt, (test_end_time - test_start_time))
    )
    # transfer into a list with each item 8000 batchsize
    trans_start_time = time.time()
    train_data_list = []
    for i, (idx, label) in enumerate(train_dataloader):
        train_data_list.append((idx, label))
    trans_end_time = time.time()
    # enumerate the transfered list
    test_start_time = time.time()
    cnt = 0
    for i, (idx, label) in enumerate(train_data_list):
        cnt += 1
    test_end_time = time.time()
    print(
        "!!!!Train_data_list(with %d items) enumerate latency: %f, transfer latency: %f"
        % (cnt, (test_end_time - test_start_time), (trans_end_time - trans_start_time)) 
    )
    comm.synchronize()

    torch.cuda.synchronize()
    train_start_time = time.time()
    skip_epoch_time = time.time()
    latency_s = 0
    latency_e = 0
    latency_t = 0
    latency_total = 0
    for epoch in range(options.epochs):
        if epoch == options.skip_epoch:
            torch.cuda.synchronize()
            skip_epoch_time = time.time()
            latency_s = 0
            latency_e = 0
            latency_t = 0
        for i, (idx, label) in enumerate(train_dataloader):
            torch.cuda.synchronize()
            step_start_time = time.time()
            if options.use_amp:
                with autocast(enabled=options.use_amp):
                    logits, time_info = model(idx)
                    label = torch.reshape(label, (-1,)).cuda()
                    optimizer.zero_grad()
                    loss = loss_fcn(logits, label)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                logits, time_info = model(idx)
                label = torch.reshape(label, (-1,)).cuda()
                optimizer.zero_grad()
                loss = loss_fcn(logits, label)
                loss.backward()
                optimizer.step()
            torch.cuda.synchronize()
            step_end_time = time.time()
            latency_s += time_info[0]
            latency_e += time_info[1]
            latency_t += (step_end_time - time_info[2])
            latency_total += (step_end_time - step_start_time)
            # if comm.get_rank() == 0:
            #     print(
            #         "[%s] [LOSS] step=%d, loss=%f, S=%f, E=%f, T=%f, Total=%f"
            #         % (
            #             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            #             i, loss.cpu().item(),
            #             time_info[0], time_info[1], (step_end_time - time_info[2]),
            #             (step_end_time - step_start_time)
            #         )
            #     )
        if comm.get_rank() == 0:
            print(
                "[%s] [LOSS] epoch=%d, loss=%f"
                % (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    epoch,
                    loss.cpu().item(),
                )
            )
    torch.cuda.synchronize()
    train_end_time = time.time()
    
    comm.synchronize()
    if comm.get_rank() == 0:
        print(
            "[TRAIN_TIME] train time is %.6f seconds"
            % (train_end_time - train_start_time)
        )
        print(
            "[EPOCH_TIME] %.6f seconds, maybe large due to not enough epoch skipped."
            % ((train_end_time - train_start_time) / options.epochs)
        )
        print(
            "[EPOCH_TIME] %.6f seconds"
            % ((train_end_time - skip_epoch_time) / (options.epochs - options.skip_epoch))
        )
        print(
            "[STEP_TIME] S = %.6f seconds, E = %.6f seconds, T = %.6f seconds"
            % (
                (latency_s / profile_steps),
                (latency_e / profile_steps),
                (latency_t / profile_steps)
            )
        )
    valid(valid_dataloader, model)


def main():
    wg.init_lib()
    torch.set_num_threads(1)
    comma = MPI.COMM_WORLD
    shared_comma = comma.Split_type(MPI.COMM_TYPE_SHARED)
    os.environ["RANK"] = str(comma.Get_rank())
    os.environ["WORLD_SIZE"] = str(comma.Get_size())
    # slurm in Selene has MASTER_ADDR env
    if "MASTER_ADDR" not in os.environ:
        os.environ["MASTER_ADDR"] = "localhost"
    if "MASTER_PORT" not in os.environ:
        os.environ["MASTER_PORT"] = "12335"
    local_rank = shared_comma.Get_rank()
    local_size = shared_comma.Get_size()
    print("Rank=%d, local_rank=%d" % (local_rank, comma.Get_rank()))
    dev_count = torch.cuda.device_count()
    assert dev_count > 0
    assert local_size <= dev_count
    torch.cuda.set_device(local_rank)
    torch.distributed.init_process_group(backend="nccl", init_method="env://")
    wm_comm = create_intra_node_communicator(
        comma.Get_rank(), comma.Get_size(), local_size
    )
    wm_embedding_comm = None
    if options.use_nccl:
        if comma.Get_rank() == 0:
            print("Using nccl embeddings.")
        wm_embedding_comm = create_global_communicator(
            comma.Get_rank(), comma.Get_size()
        )
    if comma.Get_rank() == 0:
        print("Framework=%s, Model=%s" % (options.framework, options.model))

    train_data, valid_data, test_data = graph_ops.load_pickle_data(
        options.root_dir, options.graph_name, True
    )

    dist_homo_graph = graph_ops.HomoGraph()
    use_chunked = True
    use_host_memory = False
    dist_homo_graph.load(
        options.root_dir,
        options.graph_name,
        wm_comm,
        use_chunked,
        use_host_memory,
        wm_embedding_comm,
    )
    print("Rank=%d, Graph loaded." % (comma.Get_rank(),))
    model = HomoGNNModel(
        dist_homo_graph,
        options.layernum,
        options.hiddensize,
        options.classnum,
        options.neighbors,
    )
    print("Rank=%d, model created." % (comma.Get_rank(),))
    model.cuda()
    print("Rank=%d, model movded to cuda." % (comma.Get_rank(),))
    model = DDP(model, delay_allreduce=True)
    optimizer = optim.Adam(model.parameters(), lr=options.lr)
    print("Rank=%d, optimizer created." % (comma.Get_rank(),))

    train(train_data, valid_data, model, optimizer)
    test(test_data, model)

    wg.finalize_lib()
    print("Rank=%d, wholegraph shutdown." % (comma.Get_rank(),))


if __name__ == "__main__":
    num_class = {
        'reddit' : 41,
        'products' : 47,
        'twitter' : 150,
        'papers100M' : 172,
        'ogbn-papers100M' : 172,
        'uk-2006-05' : 150,
        'com-friendster' : 100
    }
    options.classnum = num_class[options.graph_name]
    print(options)
    main()
