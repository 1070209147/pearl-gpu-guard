# Ubuntu 22.04 Pearl GPU 调度器

用于管理员授权的 Ubuntu 22.04 x86_64 八卡 NVIDIA 主机。系统服务负责启动和停止 KRig，用户通过命令行提交 GPU 请求。默认按北京时间 23:00 至次日 07:00 运行。

## 安装并启动

主机上应已安装 NVIDIA 驱动、`nvidia-smi`、`curl` 和 Git。使用有私有仓库读取权限的 GitHub 账户或部署密钥。

```bash
nvidia-smi -L
git clone https://github.com/1070209147/pearl-gpu-guard.git
cd pearl-gpu-guard
sudo bash install.sh --payout '替换为你的PRL公开收款地址'
```

参数也可填写 Kryptex Mining Username。每台主机默认用自己的主机名，也可添加 `--worker rig01`。安装脚本下载和校验官方 KRig Linux v1.7.0，并安装及启动 systemd 服务。首次安装后，

```bash
git pull --ff-only
sudo bash install.sh
```

更新沿用本机的收款地址和时段，旧版电表服务会停用。调整时可在带 `--payout` 的安装命令中加入 `--timezone Asia/Shanghai --start 23:00 --end 07:00`。先试一张卡可传入 `--eligible-pci 01:00.0`，以本机的 PCI 地址为准。停止服务使用 `sudo systemctl stop pearl-gpu-guard`，重新启动用 `sudo systemctl start pearl-gpu-guard`。

## 用户任务优先

管理员用 `sudo usermod -aG pearl-gpu ACCOUNT` 给授权账户开放请求入口。账户重新登录后，任务系统用以下命令包装在前台运行、结束时释放 GPU 的作业：

```bash
pearl-gpu-guard task -- python3 your_gpu_job.py
```

请求会在启动任务前登记。控制器终止矿工，确认进程退出和原功率设置恢复后，作业才继续。多个作业可以共用该入口，GPU 分配由自己的调度器或任务程序负责。控制器约每秒检查本机任务锁，GPU 和 Slurm 队列每 15 秒采样一次。作业都结束且显卡连续空闲至少 30 秒后，夜间可恢复挖矿。

使用 Slurm 时，可在安装命令末尾附加 `--slurm-partition gpu`。控制器报告该分区排队与运行的作业、已用时间和时限。在调度器的启动命令中也使用上面的包装器，可以让 GPU 作业等矿工退出。未接入包装器或 Slurm 的任务，要等每轮采样通过 CUDA 进程或 GPU 状态发现。

## 单卡 300W

控制器读取八张卡的 GPU 利用率、显存、温度、板卡功率及功率上限。对参与挖矿的卡，将高于 300W 的原设置降到 300W，原本更低的值继续沿用。驱动回读确认后才启动矿工。缺少可用限功率数据的卡不会参与。八张卡同时按上限运行，显卡配置合计可达 2,400W，主机其他部件和电源转换损耗另计。

设置功率上限需要管理员权限，由 systemd 内的控制器执行。KRig 以 `pearl-guard` 用户运行。原设置按 GPU UUID 保存，到停矿时恢复并回读确认。如果恢复失败，任务包装器会等待，超时后返回错误，原设置仍保存在恢复文件中。可先停止服务，再运行 `sudo pearl-gpu-guard restore-power` 重试。始终保留这份恢复文件直至驱动确认成功。

## 状态和日志

```bash
sudo pearl-gpu-guard status
sudo journalctl -u pearl-gpu-guard -n 30
nvidia-smi --query-gpu=index,power.draw,power.limit,enforced.power.limit --format=csv
```

状态包括八卡的最近采样、任务请求、Slurm 作业和矿工进程。早上截止后，服务停止挖矿、恢复功率设置并清理自身当夜详细日志和临时文件。错过截止时间时在服务下一次运行时补清理。systemd 的启停、故障与清理摘要继续保留。

本机测试可运行 `python3 -m unittest discover -s tests -v`。自动测试用模拟 NVIDIA 驱动和 Linux 子进程验证控制逻辑。首次在目标主机试跑时，运行状态命令确认每张参挖卡的设置，再提交实际 GPU 任务确认抢占与功率恢复。

参考 [KRig 官方发布](https://github.com/kryptex/krig-miner/releases/tag/v1.7.0)、[NVIDIA SMI 文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html) 和 [Slurm squeue 说明](https://slurm.schedmd.com/squeue.html)。
