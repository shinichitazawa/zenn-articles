---
title: "EKS Hybrid Nodes / Outposts / EKS Anywhere — 何が違うのか整理"
emoji: "⚖️"
type: "tech"
topics: ["aws", "eks", "outposts", "eksanywhere", "kubernetes"]
published: false
---

# はじめに

AWS が提供する「オンプレ k8s」の選択肢が 3 つに増えた。それぞれの境界線を明示する。

## 一行サマリ

- **EKS Hybrid Nodes**: 顧客所有 HW を AWS の EKS クラスタに参加させる
- **EKS on Outposts**: AWS 所有ラックを顧客 DC に置く
- **EKS Anywhere**: AWS UX を顧客 DC で完結再現

## 比較表

(TODO: docs/eks-hybrid-nodes/08-comparison.md の表を貼る)

## 決め手

(TODO: コスト・物理要件・AWS 統合度の 3 軸で選び方)

## まとめ

Raspberry Pi 利用なら EKS Hybrid Nodes 一択。
