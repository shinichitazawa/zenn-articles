#!/usr/bin/env python3
"""構成チェック: articles/*.md の必須要素を機械的に確かめる。

CLAUDE.md と zenn-article skill が課している次を見る。

  - H1 を書かない(front-matter の title: が H1 になる)
  - `## はじめに` / `## まとめ` / `## 参考` がある(`## 参考リンク` ではない)
  - title は 50 字以内
  - `## 参考` の先頭に **検証時の構成ファイル**(k8s-deploy-public へのリンク)がある

最後の 1 つは 2026-09-30 に指摘されるまで 22 本中 16 本で抜けていた。
実物が公開リポに無い記事は NO_CODE に理由付きで挙げる。**リンク先が存在しないなら
貼らない**(cite-sources: やっていないことを書かない)。
"""
import pathlib, re, sys

# 公開リポに検証コードが無い記事。理由を必ず書く。
NO_CODE = {
    "002-hybrid-vs-outposts-vs-anywhere.md": "公式ドキュメントの比較のみで検証コードが無い",
    "023-n8n-ai-agent-bedrock-vertex-azure.md": "本文中にリンクあり",
    "024-japanese-receipt-ocr-claude-nova-textract.md": "n8n のワークフローが公開リポに無い",
    "030-dogwood-temporal-policy-vs-n8n-guard.md": "dogwood は別リポ / n8n のワークフローが公開リポに無い",
    "032-n8n-jira-bidirectional-no-inbound.md": "n8n のワークフローが公開リポに無い",
    "033-constrain-the-boundary-not-the-agent.md": "設計論で検証コードが無い",
    "033-gpu-cloud-vs-buy-cost-performance.md": "コストの実測のみで検証コードが無い",
    "034-s3-protocol-deep-dive.md": "プロトコルの調査で検証コードが無い",
    "036-license-as-policy-dogwood-guardrails.md": "dogwood は別リポ",
    "037-zero-cost-explainer-video-pipeline.md": "別リポ",
}

ng = 0
for p in sorted(pathlib.Path("articles").glob("*.md")):
    lines = p.read_text().splitlines()
    fm = "\n".join(lines[1:lines.index("---", 1)])
    title = (re.search(r'^title:\s*"(.*)"\s*$', fm, re.M) or [None, ""])[1]
    in_code, h1, h2 = False, [], []
    for ln in lines:
        if re.match(r"^\s*(```|~~~)", ln): in_code = not in_code; continue
        if in_code: continue
        if m := re.match(r"^#\s+(.+)$", ln): h1.append(m.group(1))
        if m := re.match(r"^##\s+(.+)$", ln): h2.append(m.group(1).strip())
    say = lambda m: (print(f"{p.name}: {m}"), )
    if h1: say(f"H1 がある -> {h1}"); ng += 1
    if len(title) > 50: say(f"title が {len(title)} 字(50 字以内)"); ng += 1
    for need in ("はじめに", "まとめ"):
        if not any(need in h for h in h2): say(f"## {need} が無い"); ng += 1
    ref = [h for h in h2 if h.startswith("参考")]
    if ref != ["参考"]: say(f"## 参考 が無い、または名前が違う -> {ref}"); ng += 1
    if "k8s-deploy-public" not in p.read_text() and p.name not in NO_CODE:
        say("検証時の構成ファイル(k8s-deploy-public)へのリンクが無い"); ng += 1

print(f"--- {ng} 件 ---")
sys.exit(1 if ng else 0)
