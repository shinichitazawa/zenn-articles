---
title: "Raspberry Pi を EKS Hybrid Nodes で AWS の一部にする話 (背景編)"
emoji: "🍓"
type: "tech"
topics: ["aws", "eks", "raspberrypi", "kubernetes", "kro"]
published: false
---

# 動機

ローカル Raspberry Pi クラスタで K3s + Tailscale を運用してきた利用者が、なぜ EKS Hybrid Nodes 検証に動き出したのか。

## これまでの構成

(TODO: K3s + Tailscale 図、運用してきた app 一覧、抱えていた課題)

## EKS Hybrid Nodes に注目した理由

(TODO: AWS マネージド control plane、IRSA、観測の AWS 集約、Cilium eBPF をフル機能で触れる)

## 検証計画

(TODO: docs/eks-hybrid-nodes/ の構成、Pi 構成、AWS 構成、Terraform skeleton)

## 次回

Hybrid Nodes vs Outposts vs Anywhere 比較編へ。
