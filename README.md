# zenn-articles

Zenn (https://zenn.dev/shinichitazawa) で公開する記事のコンテンツリポジトリ。

下書き / 構想は private リポ `k8s-deploy` の `docs/zenn-drafts/` 側で書き、推敲完了後にこの repo の `articles/` へ複製して `published: true` に切り替えて公開する運用。

## Local preview

```bash
npx zenn preview
# http://localhost:8000 でプレビュー
```

## 新規記事作成

```bash
npx zenn new:article --slug <slug> --title "<title>" --emoji "🌱" --type tech
```

## 公開フロー

1. `docs/zenn-drafts/<slug>.md` で本文を書く (private 側)
2. 完成したらこの repo の `articles/<slug>.md` にコピー
3. `published: false` → `true` に切替
4. `npx zenn preview` で確認
5. `git add . && git commit -m "feat: <slug>" && git push`
6. Zenn 側に自動反映 (GitHub 連携設定後)

## GitHub Pages や Zenn 連携

- Zenn dashboard: https://zenn.dev/dashboard/deploys でこの repo を連携済み

## 既存記事

| slug | title | published |
|---|---|---|
| 001-raspberry-pi-meets-eks-hybrid | Raspberry Pi を EKS Hybrid Nodes で AWS の一部にする話 (背景編) | false |
| 002-hybrid-vs-outposts-vs-anywhere | EKS Hybrid Nodes / Outposts / EKS Anywhere — 何が違うのか整理 | false |
| 003-arm64-on-eks-hybrid | ARM64 (Raspberry Pi) で EKS Hybrid Nodes を動かすときの注意点 | false |
| 004-k3s-tailscale-to-eks-hybrid | K3s + Tailscale から EKS Hybrid Nodes へ — 移行設計と落とし穴 | false |
| 005-kro-vs-crossplane-personal-gitops | Kro と Crossplane、個人 GitOps ではどちらを選ぶか | false |
| 006-cilium-ebpf-on-hybrid-nodes | EKS Hybrid Nodes で Cilium eBPF を読み解く | false |

## 参考

- Zenn CLI: https://zenn.dev/zenn/articles/zenn-cli-guide
- Zenn Markdown: https://zenn.dev/zenn/articles/markdown-guide
