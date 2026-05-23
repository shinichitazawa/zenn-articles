---
title: "K3s + Tailscale から EKS Hybrid Nodes へ — 移行設計と落とし穴"
emoji: "🚚"
type: "tech"
topics: ["kubernetes", "k3s", "tailscale", "aws", "eks"]
published: false
---

# Before / After

(TODO: 構成図)

# 移行ステップ

(TODO: 1. AWS 側構築 → 2. Pi の OS 更新 → 3. K3s uninstall → 4. nodeadm install → 5. Cilium install → 6. データ移行)

# データ migration

(TODO: PostgreSQL CNPG cluster の移行、PVC の longhorn → EBS or 同じ longhorn 維持)

# Tailscale を残す理由

(TODO: bastion アクセス、SSH、CI runner)

# 落とし穴

(TODO: API endpoint policy、SG、MTU、VPN tunnel が flapping、IRSA の OIDC trust)

# コスト比較

(TODO: 月 $2 → 月 $286 の正当化)

# まとめ
