---
title: "EKS Hybrid Nodes / Outposts / EKS Anywhere — 何が違うのか整理"
emoji: "⚖️"
type: "tech"
topics: ["aws", "eks", "outposts", "eksanywhere", "kubernetes"]
published: false
---

## はじめに

AWS が提供する「オンプレで動く EKS」の選択肢が 3 つに増えた。**EKS Hybrid Nodes (2024-11 GA)**、**EKS on Outposts (2021 GA)**、**EKS Anywhere (2021 GA)**。さらに自分で組む K3s + Tailscale もある。これらの境界線が紛らわしいので整理する。

## 一行サマリ

- **EKS Hybrid Nodes**: 顧客所有 HW を AWS の EKS クラスタに参加させる
- **EKS on Outposts**: AWS 所有ラックを顧客 DC に置く
- **EKS Anywhere**: AWS UX を顧客 DC で完結再現
- **K3s + Tailscale**: 全部自前

決定的な軸は **「VPC を延伸するか」** と **「AWS HW を置くか / 自分の HW を使うか」**。

## 比較表

| 観点 | EKS Hybrid Nodes | EKS on Outposts | EKS Anywhere | K3s + Tailscale |
|---|---|---|---|---|
| 物理 HW | **顧客所有の任意 HW** | AWS 所有 42U ラック | 顧客所有 (vSphere / bare metal) | 顧客所有 |
| Control plane | AWS Region (マネージド) | Outposts 内 or Region | 顧客 DC 内 (self-hosted) | 顧客 DC 内 (self-hosted) |
| Network | 顧客 NW + S2S VPN/DX | AWS Service Link (専用回線) | 顧客 NW のみ | Tailscale Mesh (WireGuard) |
| CNI | Cilium / Calico (VPC CNI 不可) | **VPC CNI 可** | Cilium / Calico / Kube-OVN | Flannel default |
| VPC | 延伸しない | **延伸する** | 関係なし | 関係なし |
| 物理要件 | 1U Pi (5W) で可 | 42U / 数 kW / 380V | vSphere host or bare metal | 1U Pi 可 |
| 課金 | vCPU 時間単価 + VPN | 月額契約 (3 年コミット) | 年間ライセンス (Enterprise) | 電気代のみ |
| 月額目安 (個人 PoC) | ~$286 | ~$80,000+ | ~$1,200+ | ~$2 |
| AWS サービス連携 | IRSA / VPC EP 経由 | Outposts ローカル AWS サービス | なし | なし |
| ARM64 サポート | ✅ | △ (機種限定) | ✅ | ✅ |
| Pi で使える | ✅ | ❌ | △ (規模過大) | ✅ |

## 詳細

### EKS Hybrid Nodes

**設計思想**: 「顧客の自前 HW を EKS クラスタの一員として参加させる」

ポイント:

- VPC は **延伸しない**。Pod CIDR は別管理 (`RemotePodNetwork`)
- ノード認証は SSM Hybrid Activation or IAM Roles Anywhere
- CNI は VPC CNI 不可、Cilium / Calico を顧客責任で構築
- リージョン制限: GovCloud / China 以外で利用可

**向く用途**: 個人〜中小規模、オンプレと AWS の混在ワークロード、エッジ、ARM 検証

### EKS on Outposts

**設計思想**: 「AWS のラックを顧客 DC に置く」

ポイント:

- VPC が DC 内まで延伸 (Service Link)
- Outposts 内で EC2 / EBS / S3 on Outposts / RDS on Outposts / ELB が動く
- VPC CNI も使える (Outposts は AWS 物理に属する)

**向く用途**: 大企業データセンタ統合、規制 (データ residency)、低レイテンシ要件 (10ms 以下)

**個人で使えない理由**: 物理サイズ (42U / 数 kW)、契約形態 (3 年コミット数万ドル/月)、専用 AWS 設置作業

### EKS Anywhere

**設計思想**: 「EKS の構築物 (k8s + Curated packages) を顧客 DC で動かす」

ポイント:

- 顧客 DC 内の vSphere / bare metal / Snow / CloudStack
- Control plane も顧客 DC 内 (self-hosted)
- AWS Region との連携は **オプション** (基本独立)

**向く用途**: AWS 経験者がオンプレで AWS UX を再現したい場合、コンプライアンス要件で完全閉域

**問題**: Enterprise ライセンス、サポート契約必須、CNCF 純正 k8s と比べて学習価値の差が小さい

### K3s + Tailscale

**設計思想**: 「軽量 k8s + Mesh VPN で自宅クラスタ」

ポイント:

- K3s が control plane も含めて 1 バイナリ
- Tailscale で各ノード間に WireGuard mesh
- AWS との連携は完全に顧客実装

**向く用途**: 個人趣味、エッジ、学習

**問題**: AWS マネージドサービスの恩恵を受けない、運用 (etcd backup 等) が完全自前

## どれを選ぶか

| ユーザペルソナ | 推奨 |
|---|---|
| 個人/家庭ラボ、AWS 経験積みたい | **EKS Hybrid Nodes** |
| 個人/家庭ラボ、AWS は使わない | K3s + Tailscale or K3s 単独 |
| 中小企業のオフィス DC | EKS Hybrid Nodes or EKS Anywhere |
| 大企業 DC、規制 / 低レイテンシ要件 | EKS on Outposts or EKS Anywhere |
| 完全クラウド | EKS (AWS only) |

## 自分のケース

Pi 利用シナリオでは:

- Outposts は物理的・経済的に不可能 (42U ラックを家に置けない)
- EKS Anywhere は控えめに見ても overkill (個人で Enterprise ライセンス)
- K3s + Tailscale はもう経験済み
- **EKS Hybrid Nodes は完全にスイートスポット**

特に AWS の最新サービス検証として価値が高い。Cilium eBPF をフル機能で動かす場として、マネージド control plane の安定性と組み合わせられる。

## 次回予告

次回は **「ARM64 (Raspberry Pi) で EKS Hybrid Nodes を動かすときの注意点」**。multi-arch image、kernel BPF JIT、OS 選択 (Raspberry Pi OS vs Ubuntu 24.04 LTS)、各 OSS の ARM64 サポート状況を扱う。

## 参考

- [hybrid-nodes-overview](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html)
- [EKS Anywhere](https://anywhere.eks.amazonaws.com/)
- [EKS on Outposts](https://docs.aws.amazon.com/eks/latest/userguide/eks-on-outposts.html)
