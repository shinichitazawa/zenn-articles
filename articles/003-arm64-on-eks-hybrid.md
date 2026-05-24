---
title: "ARM64 (Raspberry Pi) で EKS Hybrid Nodes を動かすときの注意点"
emoji: "💪"
type: "tech"
topics: ["aws", "eks", "raspberrypi", "arm64", "cilium"]
published: false
---

## はじめに

EKS Hybrid Nodes (2024-11 GA) は ARM64 ノードに対応している。Raspberry Pi 4B (Cortex-A72) や Pi 5 (Cortex-A76) を参加させる際に踏み抜きやすい落とし穴を整理する。

## 前提

- Pi 4B (4 cores, Cortex-A72, armv8-a)
- Pi 5 (4 cores, Cortex-A76, armv8.2-a)
- いずれも 64-bit (aarch64) で k8s ノードとして利用可能

## OS 選択 — Raspberry Pi OS ではなく Ubuntu Server 24.04 LTS

**最初の落とし穴**: Raspberry Pi 公式の Raspberry Pi OS (Debian 12 ベース) は kernel 6.6 で **BTF (BPF Type Format) を default で無効**にしている。Cilium の CO-RE (Compile Once - Run Everywhere) は BTF を要求するので、これだと Cilium が起動しない。

**推奨**: Ubuntu Server 24.04 LTS arm64。kernel 6.8、cgroup v2 デフォルト、BTF デフォルト有効。

```bash
# 確認
uname -r                                  # → 6.8.0-xx-generic
stat -fc %T /sys/fs/cgroup                # → cgroup2fs
ls /sys/kernel/btf/vmlinux                # 存在すれば BTF OK
```

Raspberry Pi Imager で書込時に hostname / SSH key / WiFi 設定をカスタマイズしておくと初期セットアップが楽。

## kernel 要件チェック

EKS Hybrid Nodes + Cilium で必要な kernel feature:

| 機能 | 必要 kernel |
|---|---|
| TC BPF ingress/egress | 4.1+ |
| cgroup/connect4 (kube-proxy replacement) | 4.18+ |
| BTF / CO-RE | 5.4+ |
| Bandwidth Manager (EDT) | 4.20+ |
| WireGuard | 5.6+ |
| BPF complexity limit 1M | 5.10+ |

Ubuntu 24.04 LTS = kernel 6.8 は全て満たす。

## BPF JIT for AArch64

Linux カーネルの `arch/arm64/net/bpf_jit_comp.c` が AArch64 用 JIT。

- BPF レジスタ (R0-R10) → ARM レジスタ (x19-x28 などの callee-saved)
- 命令変換は概ね 1:1
- 性能はネイティブ C と同等

実測: Pi 4 (Cortex-A72 1.5GHz) で Cilium TC ingress を通過するパケットの latency は数 μs 以下。kube-proxy iptables モードで CPU 5-10% 食っていた処理がほぼ 0% になる。

## Multi-arch image 確認

EKS Hybrid Nodes で動かす各コンポーネントが ARM64 をサポートしているか事前確認:

```bash
# manifest list 確認
docker manifest inspect quay.io/cilium/cilium:v1.17.0 \
  | jq '.manifests[] | {arch:.platform.architecture, os:.platform.os}'
# → linux/amd64, linux/arm64 が出る

# 個別 image 確認 (crane 使用)
crane manifest --platform linux/arm64 nginx:1.27-alpine
```

主要コンポーネントの ARM64 サポート状況:

| コンポーネント | ARM64 |
|---|---|
| nginx (公式 image) | ✅ |
| postgres-operator (cnpg) | ✅ |
| prefect-server / prefect-worker | ✅ (2024+ で multi-arch) |
| trino | ✅ |
| superset | ✅ |
| backstage | ✅ (multi-arch から build) |
| k6 / k6-operator | ✅ |
| tailscale-operator | ✅ |
| cilium | ✅ |
| grafana | ✅ |
| Kro v0.9+ | ✅ |
| External Secrets Operator | ✅ |
| Pixie (PEM, Vizier) | △ (PEM は arm64 OK、Vizier 要確認) |

prefect 2.x → 3.x で ARM64 サポートが大幅改善。古い image (2022 以前) は ARM64 build がないことがあるので個別確認推奨。

## nodeSelector / tolerations パターン

Hybrid Node には EKS Control Plane が自動で taint を付ける:

```
eks.amazonaws.com/compute-type=hybrid:NoSchedule
```

ARM64 Pod を Hybrid Node にスケジュールさせたい場合:

```yaml
spec:
  nodeSelector:
    kubernetes.io/arch: arm64
  tolerations:
    - key: eks.amazonaws.com/compute-type
      operator: Equal
      value: hybrid
      effect: NoSchedule
```

逆に AWS EKS の `aws-node` (VPC CNI) DaemonSet が Hybrid Node に来てしまうと CNI 衝突するので、aws-node 側に nodeAffinity を入れる:

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: eks.amazonaws.com/compute-type
              operator: NotIn
              values: ["hybrid"]
```

## nodeadm の arm64 バイナリ

```bash
ARCH=arm64
curl -OL "https://hybrid-assets.eks.amazonaws.com/releases/latest/bin/linux/${ARCH}/nodeadm"
chmod +x nodeadm
sudo mv nodeadm /usr/local/bin/
nodeadm --version
```

nodeadm 自体は arm64 バイナリが提供されている。`nodeadm init` 経由で containerd / kubelet / SSM agent / IRSA helper も全て arm64 で自動 install される。

## Cilium ARM64 設定

Cilium は arm64 multi-arch image が公式提供。Helm values で特別な設定は不要だが、kernel BTF の有効化と cgroup v2 が前提:

```yaml
kubeProxyReplacement: true
bpf:
  masquerade: true
routingMode: tunnel
tunnelProtocol: vxlan
```

これだけで Cilium が arm64 上で動く。詳細は Cilium eBPF 編で扱う。

## ハマりやすいポイント早見表

| 症状 | 原因 | 対処 |
|---|---|---|
| `cilium status` が `iptables` fallback | BTF 未有効、kernel 古い | Ubuntu 24.04 LTS に上げる |
| Pod が ImagePullBackOff | image が amd64 only | multi-arch image を探す or 自前 build |
| nodeadm が起動しない | swap が ON | `swapoff -a` + /etc/fstab コメントアウト |
| Cilium が CrashLoop | `wireguard` kernel module 不在 | `sudo modprobe wireguard` |
| Pod が `Pending` | hybrid toleration 不足 | spec.tolerations に追加 |

## Pi のハードウェア選択

- **microSD は寿命短い**。USB SSD or NVMe HAT 推奨
- **Pi 4 4GB or 8GB**: 軽量 Pod 数個に十分
- **Pi 5 8GB**: 重い Pod (Backstage, Superset) に余裕
- **電源**: 公式アダプタ必須 (Pi 5 は 5V/5A、USB-C PD)
- **冷却**: ヒートシンク + ファン (kubelet が CPU 食う)

## 次回予告

次回は **「K3s + Tailscale から EKS Hybrid Nodes への移行設計」**。Before/After 構成図、移行ステップ、データ移行、Tailscale を残す理由、ハマりどころを扱う。

## 参考

- AWS Graviton: https://aws.amazon.com/ec2/graviton/
- Cilium ARM64 support: https://docs.cilium.io/en/stable/operations/system_requirements/
- nodeadm: https://github.com/awslabs/amazon-eks-ami/tree/main/nodeadm
