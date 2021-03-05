from prometheus_client.core import REGISTRY, GaugeMetricFamily, \
    CounterMetricFamily
from prometheus_client import start_http_server
import glob
import os
import argparse
import time
import subprocess
import re
from functools import lru_cache

try:
    import pynvml
    pynvml.nvmlInit()
    monitor_gpu = True
    print('Monitoring GPUs')
except ImportError:
    monitor_gpu = False


@lru_cache(maxsize=100)
def get_username(uid):
    command = ['/usr/bin/id', '--name', '--user', '{}'.format(uid)]
    return subprocess.check_output(command).strip().decode()


def cgroup_processes(uid, job):
    procs = []
    for i in ['step_batch', 'step_extern']:
        g = '/sys/fs/cgroup/memory/slurm/uid_{}/job_{}/{}/task_*'.format(
            uid, job, i)
        for process_file in glob.glob(g):
            with open(process_file + '/tasks', 'r') as f:
                for proc in f.readlines():
                    procs.append(proc.strip())
    return procs


def split_range(s):
    # split a range such as "0-1,3,5,10-13"
    # to 0,1,3,5,10,11,12,13
    ranges = []
    for sub in s.split(','):
        if '-' in sub:
            r = sub.split('-')
            for i in range(int(r[0]), int(r[1]) + 1):
                ranges.append(i)
        else:
            ranges.append(int(sub))
    return ranges


class SlurmJobCollector(object):
    def collect(self):
        gauge_memory_usage = GaugeMetricFamily(
            'memory_usage', 'Memory used by a job',
            labels=['user', 'job'])
        gauge_memory_max = GaugeMetricFamily(
            'memory_max', 'Maximum memory used by a job',
            labels=['user', 'job'])
        gauge_memory_limit = GaugeMetricFamily(
            'memory_limit', 'Memory limit of a job',
            labels=['user', 'job'])
        counter_core_usage = CounterMetricFamily(
            'core_usage', 'Cpu usage of cores allocated to a job',
            labels=['user', 'job', 'core'])

        if monitor_gpu:
            gauge_memory_usage_gpu = GaugeMetricFamily(
                'memory_usage_gpu', 'Memory used by a job on a GPU',
                labels=['user', 'job', 'gpu', 'gpu_type'])
            gauge_power_gpu = GaugeMetricFamily(
                'power_gpu', 'Power used by a job on a GPU in mW',
                labels=['user', 'job', 'gpu', 'gpu_type'])
            gauge_utilization_gpu = GaugeMetricFamily(
                'utilization_gpu', 'Percent of time over the past sample \
period during which one or more kernels was executing on the GPU.',
                labels=['user', 'job', 'gpu', 'gpu_type'])
            gauge_memory_utilization_gpu = GaugeMetricFamily(
                'memory_utilization_gpu', 'Percent of time over the past \
sample period during which global (device) memory was being read or written.',
                labels=['user', 'job', 'gpu', 'gpu_type'])
            gauge_pcie_gpu = GaugeMetricFamily(
                'pcie_gpu', 'PCIe throughput in KB/s',
                labels=['user', 'job', 'gpu', 'gpu_type', 'direction'])

        for uid_dir in glob.glob("/sys/fs/cgroup/memory/slurm/uid_*"):
            uid = uid_dir.split('/')[-1].split('_')[1]
            job_path = "/sys/fs/cgroup/memory/slurm/uid_{}/job_*".format(uid)
            for job_dir in glob.glob(job_path):
                job = job_dir.split('/')[-1].split('_')[1]
                mem_path = '/sys/fs/cgroup/memory/slurm/uid_{}/job_{}/'.format(
                    uid, job)
                procs = cgroup_processes(uid, job)
                if len(procs) == 0:
                    continue

                # Job is alive, we can get the stats
                user = get_username(uid)
                with open(mem_path + 'memory.usage_in_bytes', 'r') as f:
                    gauge_memory_usage.add_metric([user, job], int(f.read()))
                with open(mem_path + 'memory.max_usage_in_bytes', 'r') as f:
                    gauge_memory_max.add_metric([user, job], int(f.read()))
                with open(mem_path + 'memory.limit_in_bytes', 'r') as f:
                    gauge_memory_limit.add_metric([user, job], int(f.read()))

                # get the allocated cores
                with open('/sys/fs/cgroup/cpuset/slurm/uid_{}/job_{}/\
cpuset.effective_cpus'.format(uid, job), 'r') as f:
                    cores = split_range(f.read())
                with open('/sys/fs/cgroup/cpu,cpuacct/slurm/uid_{}/job_{}/\
cpuacct.usage_percpu'.format(uid, job), 'r') as f:
                    cpu_usages = f.read().split()
                    for core in cores:
                        counter_core_usage.add_metric([user, job, str(core)],
                                                      int(cpu_usages[core]))

                if monitor_gpu:
                    gpu_set = set()  # contains the id of the gpu
                    # Can't find the device in the cgroup whitelist with slurm
                    # lets check the file handles
                    for pid in procs:
                        for fd in glob.glob('/proc/{}/fd/*'.format(pid)):
                            try:
                                fd_path = os.readlink(fd)
                                gpu_m = re.match(r'\/dev\/nvidia(\d+)',
                                                 fd_path)
                                if gpu_m:
                                    gpu_set.add(int(gpu_m.group(1)))
                            except OSError:
                                # Too fast
                                # This fd was remove before doing the readlink
                                pass
                    for gpu in gpu_set:
                        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu)
                        gpu_type = pynvml.nvmlDeviceGetName(handle).decode()
                        gauge_memory_usage_gpu.add_metric(
                            [user, job, str(gpu), gpu_type],
                            int(pynvml.nvmlDeviceGetMemoryInfo(handle).used))
                        gauge_power_gpu.add_metric(
                            [user, job, str(gpu), gpu_type],
                            pynvml.nvmlDeviceGetPowerUsage(handle))
                        utils = pynvml.nvmlDeviceGetUtilizationRates(handle)
                        gauge_utilization_gpu.add_metric(
                            [user, job, str(gpu), gpu_type], utils.gpu)
                        gauge_memory_utilization_gpu.add_metric(
                            [user, job, str(gpu), gpu_type], utils.memory)
                        gauge_pcie_gpu.add_metric(
                            [user, job, str(gpu), gpu_type, 'TX'],
                            pynvml.nvmlDeviceGetPcieThroughput(handle, 0))
                        gauge_pcie_gpu.add_metric(
                            [user, job, str(gpu), gpu_type, 'RX'],
                            pynvml.nvmlDeviceGetPcieThroughput(handle, 1))

        yield gauge_memory_usage
        yield gauge_memory_max
        yield gauge_memory_limit
        yield counter_core_usage

        if monitor_gpu:
            yield gauge_memory_usage_gpu
            yield gauge_power_gpu
            yield gauge_utilization_gpu
            yield gauge_memory_utilization_gpu
            yield gauge_pcie_gpu

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Promtheus exporter for jobs running with Slurm \
within a cgroup')
    parser.add_argument(
        '--port',
        type=int,
        default=9798,
        help='Collector http port, default is 9798')
    args = parser.parse_args()

    start_http_server(args.port)
    REGISTRY.register(SlurmJobCollector())
    while True:
        time.sleep(60)