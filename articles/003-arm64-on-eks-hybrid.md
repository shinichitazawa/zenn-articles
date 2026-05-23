---
title: "ARM64 (Raspberry Pi) で EKS Hybrid Nodes を動かすときの注意点"
emoji: "💪"
type: "tech"
topics: ["aws", "eks", "raspberrypi", "arm64", "cilium"]
published: false
---

# 前提

(TODO: Pi 4B Cortex-A72, Pi 5 A76、Ubuntu 24.04 LTS arm64)

# Multi-arch image の確認

(TODO: docker manifest inspect、crane manifest)

# kernel BPF JIT for AArch64

(TODO: arch/arm64/net/bpf_jit_comp.c、性能、CO-RE)

# OS 選択

(TODO: なぜ Raspberry Pi OS ではなく Ubuntu 24.04 LTS なのか、BTF 有効化)

# 各 OSS の ARM64 サポート状況

(TODO: 表)

# まとめ
