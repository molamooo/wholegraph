"""
  Copyright 2022 Institute of Parallel and Distributed Systems, Shanghai Jiao Tong University
  
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at
  
      http://www.apache.org/licenses/LICENSE-2.0
  
  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
"""

import os
import datetime
from enum import Enum
import copy
import json
import math

def percent_gen(lb, ub, gap=1):
  ret = []
  i = lb
  while i <= ub:
    ret.append(i/100)
    i += gap
  return ret

def reverse_percent_gen(lb, ub, gap=1):
  ret = percent_gen(lb, ub, gap)
  return list(reversed(ret))

datetime_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
LOG_DIR='run-logs/logs_wg_' + datetime_str

class Framework(Enum):
  dgl = 0
  pyg = 1
  wg  = 2

class Model(Enum):
  sage = 0
  gcn  = 1
  gat  = 2

class Dataset(Enum):
  reddit = 0
  products = 1
  papers100M = 2
  friendster = 3
  uk_2006_05 = 4
  twitter = 5
  papers100M_undir = 6
  mag240m_homo = 7

  def __str__(self):
    if self is Dataset.friendster:
      return 'com-friendster'
    elif self is Dataset.uk_2006_05:
      return 'uk-2006-05'
    elif self is Dataset.papers100M_undir:
      return 'ogbn-papers100M'
    elif self is Dataset.mag240m_homo:
      return 'mag240m-homo'
    return self.name
  def FeatGB(self):
    return [0.522,0.912,52.96,34.22, None ,74.14,39.72, 52.96,349.27][self.value]
  def TopoGB(self):
    return [math.nan, 0.4700, 6.4326, 13.7007, math.nan , 11.3358, 5.6252, 12.4394, 13.7785][self.value]
  def short(self):
    return ['RE', 'PR', 'PA', 'FR', 'UK', 'TW', 'PAU', 'MAG'][self.value]

class CachePolicy(Enum):
  pre_sample = 0
  coll_cache = 1
  coll_intuitive = 2
  partition = 3
  part_rep = 4
  rep = 5
  coll_cache_asymm_link = 6
  clique_part = 7
  clique_part_by_degree = 8

class RunConfig:
  def __init__(self, 
               framework: Framework=Framework.dgl, 
               model: Model=Model.sage, 
               dataset:Dataset=Dataset.papers100M_undir, 
               num_workers: int=8,
               global_batch_size: int=65536,
               coll_cache_policy: CachePolicy=CachePolicy.coll_cache_asymm_link, 
               cache_percent:float=0.1, 
               logdir:str=LOG_DIR):
    self.logdir                 = logdir
    self.num_workers            = num_workers
    self.root_dir               = '/nvme/songxiaoniu/graph-learning/wholegraph'
    self.dataset                = dataset
    self.epochs                 = 4
    self.batchsize              = (global_batch_size // num_workers)
    self.skip_epoch             = 2
    self.local_step             = 1002
    self.presc_epoch            = 1
    self.neighbors              = "10,25"
    self.hiddensize             = 256
    self.layernum               = 2
    self.model                  = model
    self.framework              = framework
    self.dataloaderworkers      = 0       # number of workers for dataloader
    self.dropout                = 0.5
    self.lr                     = 0.003   # leaning rate

    self.use_collcache          = False
    self.cache_percent          = cache_percent
    self.cache_policy           = coll_cache_policy
    self.omp_thread_num         = 40
    self.empty_feat             = 0
    self.profile_level          = 3
    self.coll_cache_no_group = ""
    self.coll_cache_concurrent_link = ""
    self.num_feat_dim_hack      = None

    self.use_amp                = False
    self.use_nccl               = False
    self.unsupervised           = False

  def get_log_fname(self):
    std_out_log = f'{self.logdir}/'
    if self.unsupervised: 
      std_out_log += "unsup_"
    else: std_out_log += "sup_"
    if self.use_collcache: 
      std_out_log += "sgnn_"
    else: std_out_log += "wg_"

    std_out_log += '_'.join(
      [self.framework.name, self.model.name, self.dataset.short()] +
      [self.cache_policy.name, f'cache_rate_{round(self.cache_percent*100):0>3}'] + 
      [f'batch_size_{self.batchsize}'])
    if self.use_amp:
      std_out_log += '_amp'
    if self.coll_cache_no_group != "":
      std_out_log += f'_nogroup_{self.coll_cache_no_group}'
    if self.coll_cache_concurrent_link != "":
      std_out_log += f'_concurrent_impl_{self.coll_cache_concurrent_link}'
    return std_out_log

  def beauty(self):
    msg = 'Running '
    if self.unsupervised: 
      msg += "unsup "
    else: msg += "sup "
    if self.use_collcache: 
      msg += "sgnn "
    else: msg += "wg "
    msg += ' '.join(
      [self.framework.name, self.model.name, self.dataset.name] +
      [self.cache_policy.name, f'cache_rate {self.cache_percent}', f'batch_size {self.batchsize}'])
    if self.coll_cache_no_group != "":
      msg += f' nogroup={self.coll_cache_no_group}'
    if self.coll_cache_concurrent_link != "":
      msg += f' concurrent_link={self.coll_cache_concurrent_link}'
    return datetime.datetime.now().strftime('[%H:%M:%S]') + msg + '.'

  def form_cmd(self, durable_log=True):
    cmd_line = f'COLL_NUM_REPLICA={self.num_workers} '
    cmd_line += f'SAMGRAPH_PROFILE_LEVEL={self.profile_level} ' 
    if self.use_collcache and self.empty_feat > 0:
      cmd_line += f'SAMGRAPH_EMPTY_FEAT={self.empty_feat} '
    if self.coll_cache_no_group != "":
      cmd_line += f'SAMGRAPH_COLL_CACHE_NO_GROUP={self.coll_cache_no_group} '
    if self.coll_cache_concurrent_link != "":
      cmd_line += f' SAMGRAPH_COLL_CACHE_CONCURRENT_LINK_IMPL={self.coll_cache_concurrent_link} SAMGRAPH_COLL_CACHE_CONCURRENT_LINK=1 '
    else:
      cmd_line += f' SAMGRAPH_COLL_CACHE_CONCURRENT_LINK=0 '
    if self.num_feat_dim_hack != None:
      cmd_line += f'SAMGRAPH_FAKE_FEAT_DIM={self.num_feat_dim_hack} '

    if self.unsupervised:
      cmd_line += f'python ../../examples/gnn/gnnlab_sage_unsup.py'
    else:
      cmd_line += f'python ../../examples/gnn/gnnlab_sage_sup.py'
    
    # parameters
    cmd_line += f' --num_workers {self.num_workers} '
    cmd_line += f' --root_dir {self.root_dir} '
    cmd_line += f' --graph_name {str(self.dataset)} '
    cmd_line += f' --epochs {self.epochs} '
    cmd_line += f' --batchsize {self.batchsize} '
    cmd_line += f' --skip_epoch {self.skip_epoch} '
    cmd_line += f' --local_step {self.local_step} '
    cmd_line += f' --presc_epoch {self.presc_epoch} '
    cmd_line += f' --neighbors {self.neighbors} '
    cmd_line += f' --model {self.model.name} '
    cmd_line += f' --framework {self.framework.name} '
    if self.use_collcache:
      cmd_line += f' --use_collcache '
      cmd_line += f' --cache_percentage {self.cache_percent} '
      cmd_line += f' --cache_policy {self.cache_policy.name} '
    if self.use_nccl:
      cmd_line += ' --use_nccl'
    if self.use_amp:
      cmd_line += ' --amp'
      
    # output redirection
    if durable_log:
      std_out_log = self.get_log_fname() + '.log'
      std_err_log = self.get_log_fname() + '.err.log'
      cmd_line += f' > \"{std_out_log}\"'
      cmd_line += f' 2> \"{std_err_log}\"'
      cmd_line += ';'
    return cmd_line

  def run(self, mock=False, durable_log=True, callback = None, fail_only=False):
    '''
    fail_only: only run previously failed job. fail status is recorded in json file
    '''
    previous_succeed = False
    if fail_only:
      try:
        with open(self.get_log_fname() + '.log', "r") as logf:
          first_line = logf.readline().strip()
        if first_line == "succeed=True":
          previous_succeed = True
      except Exception as e:
        pass
      if previous_succeed:
        if callback != None:
          callback(self)
        return 0

    if mock:
      print(self.form_cmd(durable_log))
    else:
      print(self.beauty())

      if durable_log:
        os.system('mkdir -p {}'.format(self.logdir))
      status = os.system(self.form_cmd(durable_log))
      if os.WEXITSTATUS(status) != 0:
        print("FAILED!")
        if durable_log:
          self.prepend_log_succeed(False)
        return 1
      else:
        if durable_log:
          self.prepend_log_succeed(True)
      if callback != None:
        callback(self)
    return 0
  def prepend_log_succeed(self, succeed_bool):
    with open(self.get_log_fname() + '.log', "r") as logf:
      log_content = logf.readlines()
    with open(self.get_log_fname() + '.log', "w") as logf:
      print(f"succeed={succeed_bool}", file=logf)
      print("".join(log_content), file=logf)

def run_in_list(conf_list : list, mock=False, durable_log=True, callback = None):
  for conf in conf_list:
    conf : RunConfig
    conf.run(mock, durable_log, callback)

class ConfigList:
  def __init__(self):
    self.conf_list = [RunConfig()]

  def select(self, key, val_indicator):
    '''
    filter config list by key and list of value
    available key: model, dataset, cache_policy, pipeline
    '''
    newlist = []
    for cfg in self.conf_list:
      if getattr(cfg, key) in val_indicator:
        newlist.append(cfg)
    self.conf_list = newlist
    return self

  def override_arch(self, arch):
    '''
    override all arch in config list by arch
    '''
    for cfg in self.conf_list:
      cfg.arch = arch
    return self

  def override(self, key, val_list):
    '''
    override config list by key and value.
    if len(val_list)>1, then config list is extended, example:
       [cfg1(batch_size=4000)].override('batch_size',[1000,8000]) 
    => [cfg1(batch_size=1000),cfg1(batch_size=8000)]
    available key: arch, logdir, cache_percent, cache_policy, batch_size
    '''
    if len(val_list) == 0:
      return self
    orig_list = self.conf_list
    self.conf_list = []
    for val in val_list:
      new_list = copy.deepcopy(orig_list)
      for cfg in new_list:
        setattr(cfg, key, val)
      self.conf_list += new_list
    return self

  def override_T(self, key, val_list):
    if len(val_list) == 0:
      return self
    orig_list = self.conf_list
    self.conf_list = []
    for cfg in orig_list:
      for val in val_list:
        cfg = copy.deepcopy(cfg)
        setattr(cfg, key, val)
        self.conf_list.append(cfg)
    return self

  def part_override(self, filter_key, filter_val_list, override_key, override_val_list):
    newlist = []
    for cfg in self.conf_list:
      # print(cfg.cache_impl, cfg.logdir, filter_key, filter_val_list)
      if getattr(cfg, filter_key) in filter_val_list:
        # print(cfg.cache_impl, cfg.logdir)
        for val in override_val_list:
          # print(cfg.cache_impl, cfg.logdir)
          cfg = copy.deepcopy(cfg)
          setattr(cfg, override_key, val)
          newlist.append(cfg)
      else:
        newlist.append(cfg)
    self.conf_list = newlist
    return self

  def hyper_override(self, key_array, val_matrix):
    if len(key_array) == 0 or len(val_matrix) == 0:
      return self
    orig_list = self.conf_list
    self.conf_list = []
    for cfg in orig_list:
      for val_list in val_matrix:
        cfg = copy.deepcopy(cfg)
        for idx in range(len(key_array)):
          setattr(cfg, key_array[idx], val_list[idx])
        self.conf_list.append(cfg)
    return self

  def concat(self, another_list):
    self.conf_list += copy.deepcopy(another_list.conf_list)
    return self
  def copy(self):
    return copy.deepcopy(self)
  @staticmethod
  def Empty():
    ret = ConfigList()
    ret.conf_list = []
    return ret
  @staticmethod
  def MakeList(conf):
    ret = ConfigList()
    if isinstance(conf, list):
      ret.conf_list = conf
    elif isinstance(conf, RunConfig):
      ret.conf_list = [conf]
    else:
      raise Exception("Please construct fron runconfig or list of it")
    return ret

  def run(self, mock=False, durable_log=True, callback = None, fail_only=False):
    for conf in self.conf_list:
      conf : RunConfig
      conf.run(mock, durable_log, callback, fail_only=fail_only)

  def run_stop_on_fail(self, mock=False, durable_log=True, callback = None, fail_only=False):
    last_conf = None
    last_ret = None
    for conf in self.conf_list:
      conf : RunConfig
      if last_conf != None and (
                      conf.unsupervised == last_conf.unsupervised and 
                      conf.app == last_conf.app and 
                      conf.sample_type == last_conf.sample_type and 
                      conf.batch_size == last_conf.batch_size and 
                      conf.dataset == last_conf.dataset):
        if conf.cache_percent == last_conf.cache_percent and last_ret != 0:
          continue
        if conf.cache_percent > last_conf.cache_percent and last_ret != 0 :
          continue
        if conf.cache_percent < last_conf.cache_percent and last_ret == 0 :
          continue
      ret = conf.run(mock, durable_log, callback, fail_only=fail_only)
      last_conf = conf
      last_ret = ret