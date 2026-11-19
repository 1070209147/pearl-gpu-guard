# Ubuntu 22.04 Pearl GPU 调度器

用于管理员授权的 Ubuntu 22.04 x86_64 八卡 NVIDIA 主机。系统服务负责启动和停止 KRig，用户通过命令行提交 GPU 请求。默认按北京时间 23:00 至次日 07:00 运行。

## 安装并启动

主机上应已安装 NVIDIA 驱动、`nvidia-smi`、`curl` 和 Git。首次运行时填写自己的 Pearl 公开收款地址或 Kryptex Mining Username。每台主机用独立 worker 名，默认取主机名。

如果服务器带支持 DCMI 的本地 BMC，先装 `ipmitool` 并确认 `sudo ipmitool dcmi power reading` 返回有效的整机输入功率。然后在仓库目录运行：

```bash
sudo bash install.sh --payout YOUR_PUBLIC_ADDRESS --ipmi
```

有机柜 PDU、UPS 或其他整机电表时，将其读数交给软件：

```bash
sudo bash install.sh --payout YOUR_PUBLIC_ADDRESS --power-file /run/rack-power/host-watts.json
```

电表采集程序每 1–5 秒原子更新这个 JSON 文件。读数需要覆盖这台机器的所有电源输入，`sampled_at` 为采样时的 Unix 秒数，服务账号 `pearl-guard` 需要读取权限。使用 `/run` 或 `/var/lib` 下的路径。JSON 示例中的时间戳须由采集程序填写。

```json
{"watts": 183.5, "sampled_at": 1795097266.5}
```

运行安装命令会下载官方 KRig Linux v1.7.0、核对 SHA-256 并安装和启动 systemd 服务。以后 `git pull` 后执行 `sudo bash install.sh`，沿用本机配置。要调整时使用 `--timezone Asia/Shanghai --start 23:00 --end 07:00` 并带上收款和电表参数。停止服务可执行 `sudo systemctl stop pearl-gpu-guard`。

## 用户任务优先

管理员用 `sudo usermod -aG pearl-gpu ACCOUNT` 给授权账户开放请求入口。账户重新登录后，任务系统用以下命令包装在前台运行、结束时释放 GPU 的作业：

```bash
pearl-gpu-guard task -- python3 your_gpu_job.py
```

请求会在启动任务前登记。控制器检查请求锁后终止矿工并确认进程退出。此时作业才继续。多个作业可以共用该入口，资源分配仍由自己的调度器或任务程序负责。控制器约每秒处理一次请求锁和整机功耗，GPU 和队列默认每 15 秒采样一次。作业都结束且显卡连续空闲观测至少 30 秒后，夜间可恢复挖矿。

使用 Slurm 时，可在安装命令末尾附加 `--slurm-partition gpu`。控制器查看该 GPU 分区内所有排队与运行作业，并在状态中报告作业状态、已用时间和时限。管理员应确认 `pearl-guard` 能看到其他账户的作业。在调度器的启动命令中也使用上面的包装器，可以确保 GPU 作业等待矿工退出。对未接入该入口的任务，控制器只能在采样时通过已启动的 CUDA 进程或 GPU 状态发现占用。

## 300W 整机预算

控制器读取八张卡的利用率、显存、温度、板卡功率及驱动实际执行的功率上限。按实测整机空闲功耗、显卡可能增加的功耗和 15W 余量挑选可运行卡。必要时仅启动部分卡。全部卡都放不下时保持暂停。功率读数丢失、过期或达到 300W 时停矿，超预算后本夜不再自动试跑。300W 由采样和停机实现，无法消除采样间的瞬时功率峰值。需要硬上限的服务器应使用平台硬件的整机限功率功能。

软件读取现有的卡功率限制。管理员可按显卡支持的范围预先设置或通过平台策略配置硬件功率上限。八张卡均运行取决于各卡的最低可用功率、机箱底载和电源效率。

## 状态和日志

```bash
sudo pearl-gpu-guard status
sudo journalctl -u pearl-gpu-guard -n 30
```

状态包括八卡的最近采样、任务请求、Slurm 作业、整机功率与矿工进程。到早晨截止时间，服务停止挖矿并清理自己目录中的当夜详细运行日志和软件临时文件。错过截止时间时在服务下一次运行时补清理。systemd 的启停、故障与清理摘要继续保留。

本机测试可运行 `python3 -m unittest discover -s tests -v`。自动测试用模拟读数和子进程验证控制逻辑，会消耗极少资源。目标机仍需用真实电表、八张卡与实际任务验证功率和抢占。

参考 [KRig 官方发布](https://github.com/kryptex/krig-miner/releases/tag/v1.7.0)、[NVIDIA SMI 文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html) 和 [Slurm squeue 说明](https://slurm.schedmd.com/squeue.html)。
