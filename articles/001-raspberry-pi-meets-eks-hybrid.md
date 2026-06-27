---
title: "Raspberry Pi を EKS Hybrid Nodes で AWS の一部にする話 (背景編)"
emoji: "🍓"
type: "tech"
topics: ["aws", "eks", "raspberrypi", "kubernetes", "kro"]
published: false
---

## はじめに

ローカルの Raspberry Pi クラスタで K3s + Tailscale を使ってきたが、AWS が 2024-11 re:Invent で GA させた **EKS Hybrid Nodes** に乗り換える検証を始めた。本記事はその背景と全体計画を残すシリーズの第 1 回。

シリーズ予定:

1. 背景編 (本記事)
2. EKS Hybrid Nodes / Outposts / Anywhere の比較
3. ARM64 (Raspberry Pi) で動かす注意点
4. K3s + Tailscale からの移行設計
5. Kro と Crossplane、どちらを選ぶか
6. Cilium eBPF を読み解く

## これまでのローカル構成

Pi 4B 2 台 + Pi 5 1 台で K3s クラスタを組み、Tailscale で WireGuard mesh を張り、ArgoCD ApplicationSet で 15 個ほどのコンポーネント (Prefect / Trino / Superset / Backstage / etc.) を deploy していた。Pi 上で control plane (K3s server) と worker を兼用、etcd は K3s 内蔵の SQLite。

良かった点:

- 月額 ~$2 (電気代のみ)
- Tailscale Funnel で外部公開も簡単
- K3s の起動が早い (Pi 4 で 30 秒)

悩んでいた点:

- **AWS マネージドサービスの恩恵が無い**: IRSA (IAM Roles for Service Accounts) も無いし、EKS Add-on の自動アップグレードも無い
- **観測の二重化**: Prometheus / Grafana を Pi 上に置くと Pi の I/O を圧迫、AWS 側に置くと二重管理
- **データ injection**: S3 / Secrets Manager にアクセスしたい Pod に static credentials を渡す悪手
- **クラスタ作り直しが面倒**: K3s install スクリプトを叩き直すと cluster state がリセット
- **etcd backup を自前で書く**: SQLite だが定期 snapshot + S3 アップロードを cronjob で

これらを一つひとつ AWS マネージドに寄せていくよりは、**Pi を EKS クラスタの一員にしてしまう**方が早いのではないかと考え始めた。

## EKS Hybrid Nodes とは

ざっくり言うと「**顧客所有の任意ハードウェアを AWS の EKS クラスタに参加させる**」機能。AWS が 2024-11 に GA。

- Control plane は AWS が管理 (apiserver / etcd / scheduler)
- Worker node は顧客 HW (Pi, x86 サーバ, ARM64 サーバ等)
- ノード認証は **SSM Hybrid Activation** または IAM Roles Anywhere
- CNI は VPC CNI 不可、**Cilium / Calico** が必須
- リージョン制限は GovCloud / China 以外

つまり「Pi を EKS の一員にできる」。control plane の運用を AWS に丸投げできるのが大きい。

## なぜ今 Hybrid Nodes か

似た選択肢として EKS Anywhere、EKS on Outposts があるが、Raspberry Pi 利用では選択肢にならない。詳細は次回の比較編で書くが要点だけ:

- **EKS Anywhere**: 顧客 DC で control plane も含めて自前運用、AWS Region 連携はオプション。エンタープライズライセンス
- **EKS on Outposts**: AWS 所有の 42U ラックを顧客 DC に置く。物理サイズ・電力・契約 (3 年コミット数万ドル/月) で対象外
- **EKS Hybrid Nodes**: 顧客 HW を EKS の一員に。月額 vCPU 時間単価 + VPN 料金、Raspberry Pi で現実的

Pi 利用シナリオでは Hybrid Nodes 一択になる。

## 何を得たいか

検証で確認したいこと:

| 項目 | 期待 |
|---|---|
| Control plane 運用 | AWS 任せ、月額 $73 で SLA 99.95% |
| IRSA | Pod が S3 / Secrets Manager を IAM ロール経由で直接叩ける |
| 観測 | Prometheus / Grafana / Hubble を AWS 側で集約、Pi の I/O 解放 |
| 学習 | Cilium eBPF (kube-proxy replacement / WireGuard / Hubble) をフル機能で体験 |
| マルチアーキ | Pi (arm64) と AWS Graviton (arm64) または x86 混在運用 |

## 検証計画

GitHub の private リポで以下を準備した:

```text
~/work/k8s-deploy/
├── docs/eks-hybrid-nodes/        # アーキテクチャ・前提・手順・コスト試算
├── docs/learning-notes/          # Cilium eBPF, ARM64, k8s 管理ツール比較
├── eks/hybrid-nodes/             # Terraform skeleton (VPC + VPN + EKS + SSM Activation)
├── kro/                          # Kube Resource Orchestrator v0.9.2 (RGD PoC)
├── external-secrets/             # ESO + AWS Secrets Manager 連携
└── cilium/                       # Cilium 1.17 (kube-proxy replacement + WireGuard)
```

検証フェーズ:

1. AWS 側 Terraform apply (VPC / VGW / S2S VPN / EKS Cluster + remote_network_config)
2. ローカルルータ S2S VPN tunnel up
3. Pi 1 台目で nodeadm init → join
4. Cilium を kubectl apply → kube-proxy replacement 有効化
5. Pod スケジュール検証、pod-to-pod 疎通 (Pi ↔ Pi、Pi ↔ AWS EC2)
6. IRSA で Pod から S3 アクセス

実 apply に至るのはまだ先だが、設計と検証手順は十分書ける段階に来た。

## コスト予測

ざっくり試算:

- EKS Control Plane: $73/month
- Hybrid Nodes 課金: 12 vCPU × $0.020 × 720h = $172.8
- S2S VPN: $36.5
- データ転送: $3 程度
- **合計: 約 $286/month**

K3s 時代の $2 と比べて約 $284 増えるが、AWS マネージド control plane + IRSA + 観測の集約と引き換え。検証期間中はノード数を絞れば月 $80〜100 まで圧縮可能。

## 次回予告

EKS Anywhere / Outposts / Hybrid Nodes / K3s+Tailscale の 4 つを比較して、「Raspberry Pi では Hybrid Nodes 一択」になる理由を整理する。

## 関連リンク

- AWS 公式: https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html
- AWS Workshop: https://www.eksworkshop.com/docs/networking/eks-hybrid-nodes/
