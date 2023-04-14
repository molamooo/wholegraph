import os, sys, math
sys.path.append(os.getcwd()+'/../common')
from common_parser import *
from runner_helper import *
from runner import cfg_list_collector

selected_col = ['model', 'unsupervised', 'dataset_short']
selected_col += ['use_collcache', 'cache_policy', 'cache_percentage']
selected_col += ['num_worker', 'batchsize']
selected_col += ['use_amp', 'epoch_e2e_time', 'cuda_usage']

selected_col += ['Step(average) L1 sample']
# selected_col += ['Step(average) L1 recv']
selected_col += ['Step(average) L2 feat copy']
selected_col += ['Step(average) L1 train total']

selected_col += ['Time.L','Time.R','Time.C']
selected_col += ['Wght.L','Wght.R','Wght.C']
selected_col += ['Thpt.L','Thpt.R','Thpt.C']
selected_col += ['SizeGB.L','SizeGB.R','SizeGB.C']
# selected_col += ['optimal_local_rate','optimal_remote_rate','optimal_cpu_rate']
selected_col += ['coll_cache:local_cache_rate']
selected_col += ['coll_cache:remote_cache_rate']
selected_col += ['coll_cache:global_cache_rate']

cfg_list_collector = (cfg_list_collector.copy()
  # .select('use_collcache', [True])
  # .select('dataset', [Dataset.papers100M_undir])
)

def div_nan(a,b):
  if b == 0:
    return math.nan
  return a/b

def max_nan(a,b):
  if math.isnan(a):
    return b
  elif math.isnan(b):
    return a
  else:
    return max(a,b)

def handle_nan(a, default=0):
  if math.isnan(a):
    return default
  return a
def zero_nan(a):
  return handle_nan(a, 0)
if __name__ == '__main__':
  bench_list = [BenchInstance().init_from_cfg(cfg) for cfg in cfg_list_collector.conf_list]
  for inst in bench_list:
    inst : BenchInstance
    try:
      inst.vals['Step(average) L1 train total'] = inst.get_val('Step(average) L1 convert time') + inst.get_val('Step(average) L1 train')
      # when cache rate = 0, extract time has different log name...
      inst.vals['Step(average) L2 feat copy'] = max_nan(inst.get_val('Step(average) L2 cache feat copy'), inst.get_val('Step(average) L2 extract'))

      # per-step feature nbytes (Remote, Cpu, Local)
      inst.vals['Size.A'] = inst.get_val('Step(average) L1 feature nbytes')
      inst.vals['Size.R'] = handle_nan(inst.get_val('Step(average) L1 remote nbytes'), 0)
      inst.vals['Size.C'] = handle_nan(inst.get_val('Step(average) L1 miss nbytes'), inst.vals['Size.A'])
      inst.vals['Size.L'] = inst.get_val('Size.A') - inst.get_val('Size.C') - inst.get_val('Size.R')

      inst.vals['SizeGB.R'] = inst.get_val('Size.R') / 1024 / 1024 / 1024
      inst.vals['SizeGB.C'] = inst.get_val('Size.C') / 1024 / 1024 / 1024
      inst.vals['SizeGB.L'] = inst.get_val('Size.L') / 1024 / 1024 / 1024

      # per-step extraction time
      inst.vals['Time.R'] = handle_nan(inst.get_val('Step(average) L3 cache combine remote'))
      inst.vals['Time.C'] = handle_nan(inst.get_val('Step(average) L3 cache combine_miss'), inst.get_val('Step(average) L2 extract'))
      inst.vals['Time.L'] = handle_nan(inst.get_val('Step(average) L3 cache combine cache'))

      # per-step extraction throughput (GB/s)
      inst.vals['Thpt.R'] = div_nan(inst.get_val('Size.R'), inst.get_val('Time.R')) / 1024 / 1024 / 1024
      inst.vals['Thpt.C'] = div_nan(inst.get_val('Size.C'), inst.get_val('Time.C')) / 1024 / 1024 / 1024
      inst.vals['Thpt.L'] = div_nan(inst.get_val('Size.L'), inst.get_val('Time.L')) / 1024 / 1024 / 1024

      # per-step extraction portion from different source
      inst.vals['Wght.R'] = div_nan(inst.get_val('Size.R'), inst.get_val('Size.A')) * 100
      inst.vals['Wght.C'] = div_nan(inst.get_val('Size.C'), inst.get_val('Size.A')) * 100
      inst.vals['Wght.L'] = 100 - inst.get_val('Wght.R') - inst.get_val('Wght.C')
    except Exception as e:
      print("Error when " + inst.cfg.get_log_fname() + '.log')
  with open(f'data.dat', 'w') as f:
    BenchInstance.print_dat(bench_list, f, selected_col)