---
title: "K3s + Tailscale から EKS Hybrid Nodes へ — 移行設計と落とし穴"
emoji: "🚚"
type: "tech"
topics: ["kubernetes", "k3s", "tailscale", "aws", "eks"]
published: false
---

## はじめに

ローカル Raspberry Pi で K3s + Tailscale 構成を 1 年以上運用してきたが、EKS Hybrid Nodes に移行する。本記事は移行ステップと、設計上の判断と落とし穴を整理する。

## Before / After

### Before (K3s + Tailscale)

```text
[ローカル LAN]
  Pi#1 (K3s server + worker)
  Pi#2 (K3s worker)
  Pi#3 (K3s worker)
  └ Tailscale Mesh (WireGuard)
        ├ 各ノード間
        └ 自分のラップトップ → kubectl
```

- Control plane: K3s server on Pi#1 (self-hosted、etcd は SQLite)
- 認証: static kubeconfig + Tailscale ACL
- Pod NW: Flannel VXLAN over Tailscale
- Service: kube-proxy iptables
- 観測: Prometheus on Pi → Grafana on Pi
- 月額: ~$2 (電気代のみ)

### After (EKS Hybrid Nodes)

```text
[AWS Region ap-northeast-1]
  EKS Control Plane (managed)
    │
    │ kubelet 443 (VPN)
    ▼
[Site-to-Site VPN]
    │
    ▼
[ローカル LAN]
  Pi#1 (EKS worker / hybrid)
  Pi#2 (EKS worker / hybrid)
  Pi#3 (EKS worker / hybrid)
  └ Cilium VXLAN + WireGuard
```

- Control plane: AWS マネージド
- 認証: OIDC + IRSA + SSM Hybrid Activation
- Pod NW: Cilium VXLAN + WireGuard
- Service: Cilium eBPF (kube-proxy replacement)
- 観測: Grafana on AWS、Pi の metrics は scrape
- 月額: ~$286 (Control plane + Hybrid 課金 + VPN)

## 移行ステップ

1. **AWS 側構築** (Terraform): VPC / VGW / S2S VPN / Customer Gateway / EKS Cluster (`remote_network_config`) / SSM Hybrid Activation / IAM Role
2. **ローカルルータ S2S VPN 設定**: tunnel up + BGP 経路交換
3. **Pi の OS 更新**: Raspberry Pi OS → Ubuntu 24.04 LTS arm64 に書き換え
4. **K3s uninstall**: `/usr/local/bin/k3s-uninstall.sh`
5. **nodeadm install**: `nodeadm init --config-source file:///etc/nodeadm/nodeConfig.yaml`
6. **Cilium 投入**: kubectl apply で kube-proxy replacement + VXLAN + WireGuard
7. **データ移行**: PostgreSQL CNPG cluster、PVC、Secrets
8. **ApplicationSet 再配置**: `targetRevision: main`、nodeSelector を arm64 + hybrid toleration に
9. **動作確認**: Pod スケジュール、pod-to-pod、IRSA で Pod ↔ AWS API

## データ migration

### PostgreSQL (CNPG cluster)

- 旧クラスタで `kubectl cnpg backup my-pg-cluster` → S3 にアーカイブ
- 新クラスタで `Cluster.spec.bootstrap.recovery.backup` から復元
- 切り替え時間中の書込み損失を許容するか、論理レプリケーションで cutover を最小化するか

### PVC

- Pi の k3s では local-path-provisioner (local SSD) を使っていた
- EKS Hybrid Node では同じ Pi 上の local-path を継続利用可能 (Longhorn / OpenEBS 等の分散 PV は別途検討)
- **EBS CSI は Hybrid Node では使えない** (EBS は AWS 物理依存)

### Secrets

- 旧クラスタの Secret は kubectl で export → 新クラスタに import、または
- **External Secrets Operator + AWS Secrets Manager に集約** (推奨)
- Tailscale OAuth token、ghcr.io PAT 等を AWS Secrets Manager に保存、ExternalSecret で参照

## Tailscale を残す理由

EKS Hybrid Nodes 移行後も Tailscale は **完全には消さない**。残す目的:

1. **bastion アクセス**: AWS EKS Cluster の private endpoint に kubectl したいとき、Tailscale 経由で VPN を補完
2. **SSH 経路**: Pi に SSH するのにグローバル IP / DDNS より楽
3. **CI runner**: GitHub Actions self-hosted runner を Pi で動かしている場合、Tailscale でアクセス
4. **fail-safe**: S2S VPN が落ちた時の代替経路 (debug 用)

つまり Tailscale を Pod NW から外し、ホスト OS 層の管理用 mesh として残置する。Cilium と Tailscale が同じ host 上で共存する。

## 落とし穴

### API endpoint policy の罠

EKS Cluster の API endpoint を **"Public and Private"** にすると、kubelet が public IP に解決して join に失敗するケースがある。

→ **Private only** で開始 (VPN 必須)、kubectl は AWS bastion 経由 or リモートから VPN 経由。

### CIDR 重複

VPC / EKS service / on-prem node / on-prem pod の CIDR が相互に重複していないことを確認:

| CIDR | 例 |
|---|---|
| vpc_cidr | 10.226.0.0/16 |
| EKS service CIDR | 172.20.0.0/16 (固定) |
| remote_node_cidr | 192.168.10.0/24 (ローカル LAN) |
| remote_pod_cidr | 100.64.0.0/16 (CGNAT 帯) |

重複していると `RemoteNetworkConfig` 設定時に EKS API が reject、または apply 後に sync ループ。

### SG inbound 不足

AWS 側 worker SG に Hybrid Node からの inbound を許可:

| port/proto | 用途 |
|---|---|
| 443/TCP | kubelet → apiserver |
| 4240/TCP | cilium-agent cluster health |
| 8472/UDP | VXLAN |
| 51871/UDP | WireGuard |

### MTU

VXLAN + WireGuard + S2S VPN (IPsec) で encap overhead が積み上がる:

- 物理 MTU 1500
- VXLAN -50
- WireGuard -60
- IPsec -40
- **Pod MTU 1350** くらいまで下げると安定

### IRSA の OIDC trust

Pod が AWS API を叩くために IRSA を使う場合、IAM Role の trust policy に EKS Cluster の OIDC provider を登録する必要がある。

```hcl
resource "aws_iam_role" "pod_role" {
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::ACCOUNT:oidc-provider/<EKS_OIDC_ISSUER>"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "<EKS_OIDC_ISSUER>:sub" = "system:serviceaccount:<ns>:<sa>"
        }
      }
    }]
  })
}
```

Hybrid Node 上の Pod も同じ OIDC trust で動く。

## コスト比較

| 構成 | 月額 |
|---|---|
| K3s + Tailscale (Pi 3 台) | ~$2 (電気代のみ) |
| EKS Hybrid Nodes (Pi 3 台) | ~$286 |
| 差分 | +$284 |

正当化:

- AWS マネージド Control Plane の運用解放 (etcd backup, upgrade, SLA)
- IRSA で Pod から AWS API を直接叩ける (S3 / SecretsManager 等)
- Cilium eBPF 全機能 (WireGuard, Hubble, kube-proxy replacement) を本番運用環境として体験
- AWS 経験を 1〜2 ヶ月で大幅に深められる

技術書 + AWS 認定講座より遥かに学習効率が良い、と考えると月 $286 は安い。

## 検証中の cleanup

```bash
# Pi 側
sudo nodeadm uninstall --skip phase=k8s-cleanup
sudo systemctl disable nodeadm-run

# AWS 側
aws ssm deregister-managed-instance --instance-id mi-...
terraform destroy
```

## 次回予告

次回は **「Kro と Crossplane、どちらを選ぶか」**。Hybrid Nodes 移行後、AWS リソース (IRSA Role / S3 Bucket / Lambda) を K8s API で管理する選択肢を比較する。

## 参考

- [hybrid-nodes-prereqs](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-prereqs.html)
- [hybrid-nodes-networking](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-networking.html)
- [nodeadm](https://github.com/awslabs/amazon-eks-ami/tree/main/nodeadm)
