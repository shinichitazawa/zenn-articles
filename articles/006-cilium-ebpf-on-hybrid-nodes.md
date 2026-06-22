---
title: "EKS Hybrid Nodes で Cilium eBPF を読み解く"
emoji: "🐝"
type: "tech"
topics: ["cilium", "ebpf", "kubernetes", "networking", "aws"]
published: false
---

## はじめに

EKS Hybrid Nodes は VPC CNI を使えない。CNI は実質 Cilium 一択になる。**それはなぜか** を eBPF レベル、つまりカーネル内で何が起きているかまで掘り下げる。シリーズの最終回。

## VPC CNI が使えない理由

EKS の標準 CNI は `amazon-vpc-cni-k8s` (aws-node DaemonSet)。これは:

1. Pod 1 つにつき AWS ENI 上の **Secondary IP** を 1 つ割当てる
2. AWS API `AssignPrivateIpAddresses` を呼んで ENI に IP を追加
3. veth pair を作り Pod の network namespace を ENI と紐付ける

問題: **AWS ENI Attach API は AWS が物理的に所有する EC2 インスタンスにしか効かない**。Raspberry Pi は AWS にとって認知できない物理マシンなので ENI を持てない。

→ オーバーレイ CNI が必要 → **Cilium**(本検証で採用) または **Calico**。

Cilium を選ぶ理由:

1. EKS Hybrid Nodes 公式 add-on として AWS が積極サポート
2. kube-proxy replacement で iptables を完全に切り捨てられる (Pi の low-power CPU で iptables チェーン scan を回避)
3. ARM64 multi-arch ビルドが提供されている
4. Hubble による Pod-to-Pod 可視化が観測の入り口になる

## Cilium のアーキテクチャと eBPF 配置

```text
┌────────────────────────────────────────────────────────┐
│ User space (per node)                                  │
│ ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│ │ cilium-agent │  │ cilium-      │  │ hubble-relay │  │
│ │  (DaemonSet) │  │  operator    │  │              │  │
│ └──────┬───────┘  └──────────────┘  └──────────────┘  │
│        │ load eBPF                                    │
├────────┼───────────────────────────────────────────────┤
│ Linux kernel (eBPF programs attached at hooks)        │
│                                                        │
│  XDP (NIC driver) ──┬─→ DDoS drop, prefilter          │
│                     │                                   │
│  TC (qdisc clsact) ─┴─→ ingress/egress (主戦場)        │
│                                                        │
│  cgroup/connect4 ──→ socket-level service redirect    │
│  cgroup/connect6                                       │
│                                                        │
│  sk_msg, sock_ops ──→ sockmap optimization            │
│                                                        │
│  BPF maps:                                            │
│   cilium_lxc, cilium_lb4_*, cilium_ct4_*,             │
│   cilium_ipcache, cilium_tunnel_map, cilium_policy_*  │
└────────────────────────────────────────────────────────┘
```

`cilium-agent` は kubelet から Pod ライフサイクルイベントを受けて:

1. `veth` ペア (host 側 = `lxc<endpoint-id>`, container 側 = `eth0`) を作成
2. host 側 veth の TC ingress/egress に eBPF プログラムを attach
3. endpoint metadata を `cilium_lxc` map に書き込む
4. Identity (security identity, uint32) を `cilium_ipcache` に IP→identity でマッピング
5. Policy enforcement 用の per-endpoint policy map を populate

## BPF プログラムタイプと Cilium の用途

| プログラムタイプ | Cilium での用途 | hook |
|---|---|---|
| `BPF_PROG_TYPE_XDP` | DDoS prefilter (drop early)、optional L3 LB | NIC driver (RX 前) |
| `BPF_PROG_TYPE_SCHED_CLS` (TC) | **主戦場**: ingress/egress、NAT、overlay encap/decap | qdisc clsact |
| `BPF_PROG_TYPE_CGROUP_SOCK_ADDR` | **kube-proxy replacement の起点** | cgroup v2 |
| `BPF_PROG_TYPE_SK_MSG` | sockmap で同一ノード Pod-to-Pod を userspace 経由なし redirect | sockmap |
| `BPF_PROG_TYPE_LWT_IN/OUT` | encap/decap (light-weight tunnel) | route output |

Verifier の制約 (stack 512B、命令数上限) のため、Cilium は **tail calls** で program を分割している (`tail_handle_ipv4`, `tail_handle_arp` 等)。

## 主要 BPF maps

| map | type | 役割 |
|---|---|---|
| `cilium_lxc` | HASH | endpoint metadata (lxc_id → ipv4/mac/node_id/ifindex) |
| `cilium_lb4_services_v2` | HASH | Service ClusterIP/NodePort → backend_id |
| `cilium_lb4_backends_v3` | HASH | backend_id → pod_ip:port |
| `cilium_ct4_global` | LRU_HASH | connection tracking (5-tuple → state) |
| `cilium_ipcache` | LPM_TRIE | IP/prefix → security identity, remote node |
| `cilium_tunnel_map` | HASH | remote pod CIDR → tunnel endpoint VTEP |
| `cilium_policy_<endpoint>` | HASH | per-endpoint policy |
| `cilium_events` | PERF_EVENT_ARRAY | Hubble の packet drop / flow event ring buffer |

確認:

```bash
bpftool map show | grep cilium
bpftool map dump name cilium_ipcache | head
bpftool map dump name cilium_tunnel_map
```

## kube-proxy replacement のパケットパス

通常の EKS では `kube-proxy` が iptables (or IPVS) で ClusterIP → Pod IP の DNAT を行う。Cilium は **kube-proxy を完全に置換**し、socket-level でカーネル内 DNAT を実現する。

### ClusterIP 通信 (Pod → Service)

```text
Pod プロセス
   │ connect(10.96.0.10:80)  ← Service ClusterIP
   ▼
[cgroup/connect4 hook] eBPF program
   │ 1. cilium_lb4_services_v2 lookup (10.96.0.10:80)
   │ 2. Maglev consistent hashing で backend 選択
   │ 3. cilium_lb4_backends_v3 lookup → 100.64.1.5:8080
   │ 4. sockaddr_in を 100.64.1.5:8080 に書き換える (in-kernel DNAT)
   ▼
TCP stack が SYN を送信 (宛先は既に backend IP)
```

ポイント:

- **SYN が出る前に DNAT が完了** している。netfilter conntrack を一切経由しない
- iptables INPUT/OUTPUT/FORWARD/PREROUTING/POSTROUTING のチェーン scan ゼロ
- Pi の Cortex-A72/A76 で iptables が CPU 5-10% を食う問題が消える

### Maglev consistent hashing

Service backend 選択は **Maglev** (Google 2016):

```text
N backends を M スロット (M ≫ N、Cilium デフォルト 16381) の table にマップ
   ・各 backend は preference list を持つ
   ・round-robin で空きスロットに自分の preference を埋める
```

Lookup: `backend = maglev_table[jhash(5-tuple) % M]`

利点:

- Consistent: backend 追加/削除時に **約 1/N のスロットだけ変動**
- O(1) lookup、均一分散

## Pod-to-Pod パケットパス (Hybrid Nodes 固有)

### 同一ノード

```text
Pod A (veth eth0 ↔ lxcA)
   ↓ packet
[TC egress on lxcA]
   │ cilium_lxc[A] → SrcID
   │ cilium_ipcache[dst_ip] → DstID, same_node? yes
   │ policy check: cilium_policy_B[SrcID][port] → allow?
   │ skb_redirect → lxcB ingress
   ↓
[TC ingress on lxcB] → eth0 of Pod B
```

`skb_redirect` で kernel net stack の routing を bypass。**veth → veth で直接届く**。

### クロスノード (オンプレ ↔ AWS hybrid)

**ここが Hybrid Nodes の真髄**。AWS VPC route table に on-prem pod CIDR を伝搬する手段は **無い** (VPC CNI なら自然に解決するが Hybrid では使えない)。

```text
Pod A (オンプレ) → TC egress lxcA → policy + ipcache
   │ cilium_tunnel_map[C_pod_cidr] → node_C_internal_ip (AWS 側)
   │ VXLAN encap → UDP:8472 宛 AWS_node_C_IP
   ▼
host eth0 → 自宅ルータ → Site-to-Site VPN → AWS VGW → VPC
   ▼
AWS 側 ENI (node C) → kernel
   ▼
TC ingress on eth0 → VXLAN decap → cilium_ipcache → skb_redirect → lxcC
   ▼
Pod C
```

なぜ Tunnel Mode (VXLAN/Geneve) が事実上必須か:

- Direct Routing は **AWS VPC route table に `remote_pod_cidr` を入れる** か **BGP で広告する** 必要があるが、AWS 側でこれを許可していない
- Tunnel Mode なら **VPC は VXLAN UDP パケットを node 間で運ぶだけ** で、内側の Pod IP は VPC からは見えない → VPC ルーティングと完全独立

## kube-proxy replacement と `aws-node` の関係

EKS は default で `kube-system` ns に `aws-node` (VPC CNI) DaemonSet をデプロイする。Hybrid Node 上では VPC CNI を起動させてはいけない:

- EKS Control Plane は Hybrid Node に `eks.amazonaws.com/compute-type=hybrid` を付与
- aws-node DaemonSet の affinity を以下のように設定:

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

同様に `kube-proxy` も Hybrid Node には不要 (Cilium が完全置換) → 同じ NotIn=hybrid。

## Encryption: WireGuard

Hybrid Nodes は VPN 経由でも公衆網経由でも、Pod-to-Pod 通信を **アプリケーションレイヤで暗号化** すべき。Cilium はこれを kernel space で透過的に実施:

```text
TC egress lxcA → policy → ipcache → BPF が cilium_wg0 に skb_redirect
   ↓
WireGuard カーネルモジュールが ChaCha20-Poly1305 で暗号化、UDP:51871 にカプセル化
   ↓
host eth0 → 経路上は普通の UDP パケット
```

設定:

```yaml
encryption:
  enabled: true
  type: wireguard
  nodeEncryption: true
```

`cilium-agent` が起動時に WG キーペア生成、`CiliumNode` CRD で public key 配布、各ノードが `cilium_wg0` の peer 一覧を構成。鍵交換は K8s API 経由なので外部 PKI 不要。

## Identity-based Network Policy

Cilium の独自性のひとつ。IP ベースではなく **Pod ラベルから導出した numeric security identity** で policy enforce する:

1. Pod のラベル (`app=foo,env=prod`) → controller が hash 計算 → identity (uint32)
2. identity は `cilium_ipcache` の値として `IP → (identity, remote_node)` でマッピング
3. 各 endpoint には per-endpoint policy map が attach
4. TC ingress でパケット到着時:
   - source IP → ipcache → source_identity
   - cilium_policy_local[source_identity][dport][proto] → allow/deny

CiliumNetworkPolicy 例:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: db-allow-webapp
spec:
  endpointSelector:
    matchLabels:
      role: db
  ingress:
    - fromEndpoints:
        - matchLabels:
            role: webapp
            env: prod
      toPorts:
        - ports: [{ port: "5432", protocol: TCP }]
```

これは `webapp+prod` の identity が `db` の identity 5432/TCP を許可、という BPF map entry に解決される。

## Hubble 可視化

```text
TC hook → eBPF が cilium_events に flow event を perf_event_output
   ↓
cilium-agent が perf ring buffer を mmap で read
   ↓
hubble-relay (DaemonSet 外の Deployment) がクラスタ全体を集約
   ↓
hubble UI / hubble CLI
```

flow event の中身:

- 5-tuple、direction、verdict (allow/deny/error)
- source/dest identity (label まで遡れる)
- L7 metadata (HTTP method/path、DNS query 等、Envoy 経由時)

## EKS Hybrid Nodes 固有の論点

### `remoteNodeNetworks` / `remotePodNetworks`

EKS Cluster の `remoteNetworkConfig` block で:

```hcl
remote_network_config {
  remote_node_networks { cidrs = ["192.168.10.0/24"] }
  remote_pod_networks  { cidrs = ["100.64.0.0/16"] }
}
```

これを設定すると EKS Control Plane が:

- AWS 側 worker SG の inbound に自動で `remote_pod_cidr` からの kubelet 443 を許可
- AWS 側 Pod から `remote_pod_cidr` 宛のパケットを EKS 内 VPC route で適切に処理
- ENI Trunk 等の機能を Hybrid Node 用に無効化

### SG inbound

| port/proto | 用途 |
|---|---|
| `443/TCP` | kubelet → apiserver |
| `4240/TCP` | cilium-agent → cilium-agent (cluster health) |
| `8472/UDP` | VXLAN |
| `51871/UDP` | WireGuard |

### kernel 要件

- Linux 5.4 以上 (`KUBE_PROXY_REPLACEMENT=true` には 5.10+ 推奨)
- BTF (Compile Once - Run Everywhere): `CONFIG_DEBUG_INFO_BTF=y` 必須
- Ubuntu 24.04 LTS = 6.8 系で完全に満たす
- cgroup v2 が必須 (cgroup/connect4 hook のため)

## 検証コマンド

```bash
cilium status --verbose
cilium-dbg bpf lb list                   # cilium_lb4_services_v2 ダンプ
cilium-dbg bpf ct list global            # conntrack
cilium-dbg bpf ipcache list              # IP → identity
cilium-dbg bpf tunnel list               # オーバーレイ宛先
cilium-dbg endpoint list

# kernel 側
bpftool prog show | grep cilium
bpftool map show | grep cilium
bpftool map dump name cilium_ipcache | head

# tc filter
tc filter show dev lxc<endpoint-id> ingress

# overlay
ip -d link show type vxlan
ss -uanp | grep 8472

# Hubble
hubble status
hubble observe --follow
hubble observe --to-pod prefect/prefect-server
```

## まとめ

EKS Hybrid Nodes で Cilium を使う必然性は、VPC CNI が物理 ENI 依存だから。Cilium は eBPF で:

- kube-proxy を完全置換 (cgroup/connect4 hook で socket-level DNAT)
- VXLAN tunnel で AWS VPC とオンプレ Pod をフラットに繋ぐ
- WireGuard でカーネル内透過暗号化
- Identity-based policy で IP に依存しない動的セキュリティ
- Hubble で全 flow を可視化

これらを **マネージド control plane (AWS) + 自前 worker (Pi)** という構成で動かせるのが EKS Hybrid Nodes の魅力。Pi の低消費電力 ARM64 と Cilium の eBPF datapath の組み合わせは、個人検証に最適。

## シリーズ完

6 本のシリーズで「自宅 Pi を EKS Hybrid Nodes で AWS の一部にする」検証準備を網羅した。実 apply 結果が出たら、別シリーズとして書く予定。

## 参考

- Cilium docs: https://docs.cilium.io
- eBPF.io: https://ebpf.io
- "Maglev: A Fast and Reliable Software Network Load Balancer" (Google, NSDI 2016)
- "Learning eBPF" (Liz Rice, O'Reilly, 2023)
- Cilium source: https://github.com/cilium/cilium
- [hybrid-nodes-overview](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html)
