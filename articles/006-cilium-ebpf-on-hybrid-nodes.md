---
title: "EKS Hybrid Nodes で Cilium eBPF を読み解く"
emoji: "🐝"
type: "tech"
topics: ["cilium", "ebpf", "kubernetes", "networking", "aws"]
published: false
---

# はじめに

EKS Hybrid Nodes は VPC CNI を使えない。CNI は実質 Cilium 一択。それは「なぜ」を eBPF レベルで説明する。

# VPC CNI が使えない理由

(TODO: ENI Attach API が AWS 物理に依存、Hybrid Node は AWS にとって不可視)

# Cilium のアーキテクチャ

(TODO: cilium-agent / operator、eBPF プログラムの配置、TC / XDP / cgroup hook)

# BPF プログラムタイプと map

(TODO: 主要 program と map の表)

# kube-proxy replacement の仕組み

(TODO: cgroup/connect4 hook、Maglev hashing、socket-level DNAT)

# Pod-to-Pod パケットパス

(TODO: 同一ノード、クロスノード VXLAN、Hybrid 越え)

# WireGuard 暗号化

(TODO: cilium_wg0 にカーネル内 redirect)

# Identity-based Policy

(TODO: numeric identity、cilium_ipcache、per-endpoint policy map)

# 検証コマンド

(TODO: cilium-dbg bpf、bpftool、hubble observe)

# まとめ
