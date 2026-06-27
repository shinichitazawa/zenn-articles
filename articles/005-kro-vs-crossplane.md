---
title: "Kro と Crossplane、どちらを選ぶか — 複合リソースと宣言的クラウド管理"
emoji: "🧩"
type: "tech"
topics: ["kubernetes", "kro", "crossplane", "kustomize", "iac"]
published: false
---

## はじめに

Kubernetes マニフェストを宣言的に管理していると、「複合リソースの取り扱い」と「クラウドリソースの宣言的管理」で詰まる場面があります。これらを解決する OSS として **Kro (Kube Resource Orchestrator)** と **Crossplane** があります。両者は重なる領域があるが思想が異なります。本記事では両者の仕組みと選び方を整理します。

想定読者は、Kustomize / Helm での複合リソース管理に限界を感じ、上位の抽象化レイヤを検討している中級者。

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 複合リソース・クラウドリソース管理で直面する問題

Kustomize + ArgoCD で多数のアプリを deploy していると、以下の問題に直面します。

1. **複合リソースの煩雑さ**: Web app 1 個を deploy するのに Deployment + Service + ConfigMap + ServiceAccount + (将来 IRSA Role + S3 Bucket) を別々の YAML で書き、ApplicationSet で展開する手間
2. **values の重複**: Helm chart の overlay で同じ値を environment 毎に書き直す
3. **AWS リソースの管理**: EKS Hybrid Nodes 移行後、IAM Role や S3 Bucket を K8s API で管理したくなる (Terraform から離れたい)

## Kro (Kube Resource Orchestrator)

**[Kro](https://kro.run/)** は 2024 年後半に公開された比較的新しい OSS。最新 v0.9.2 (2026-05 時点)。

### 仕組み

**`ResourceGraphDefinition` (RGD)** という CRD で「高レベル API」を定義し、内部で K8s リソース (or ACK Controller 経由で AWS リソース) を生成する:

```yaml
apiVersion: kro.run/v1alpha1
kind: ResourceGraphDefinition
metadata:
  name: simple-webapp
spec:
  schema:
    apiVersion: v1alpha1
    kind: SimpleWebApp
    spec:
      name: string
      image: string | default="nginx:1.27-alpine"
      replicas: integer | default=1
  resources:
    - id: deployment
      template:
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: ${schema.spec.name}
        spec:
          replicas: ${schema.spec.replicas}
          # ...
    - id: service
      template:
        # ...
```

これを apply すると `SimpleWebApp` という新しい CRD が生まれ、ユーザは以下のように 1 つの spec で複合リソースを deploy できる:

```yaml
apiVersion: kro.run/v1alpha1
kind: SimpleWebApp
metadata:
  name: hello
spec:
  name: hello
  image: nginx:1.27-alpine
  replicas: 1
```

Kro controller が裏で Deployment + Service + ConfigMap を生成します。

### 特徴

- **K8s ネイティブ** (CRD ベース、verifier/JIT 不要)
- 軽量 (controller 100m CPU / 64Mi RAM で動く)
- ACK Controllers と組合せで AWS リソースも RGD で管理可能
- v0.9.x、alpha API なので破壊的変更の可能性あり

### Install

```bash
helm install kro oci://registry.k8s.io/kro/charts/kro \
  --namespace kro-system \
  --create-namespace \
  --version=0.9.2
```

## Crossplane

**[Crossplane](https://docs.crossplane.io/)** は CNCF Sandbox → Incubating の OSS、2018 年から発展、エコシステム成熟。

### 仕組み

- **Provider** が各クラウド (AWS / GCP / Azure / GitHub / etc.) の API を CRD として K8s に橋渡し
- **Composition** で「複数 Provider リソースを束ねた抽象」を定義
- **CompositeResourceClaim (XRC)** でユーザがそれを apply

```yaml
apiVersion: example.org/v1alpha1
kind: WebAppClaim
metadata:
  name: my-app
spec:
  bucketName: my-app-bucket
  region: ap-northeast-1
```

これが裏で AWS S3 Bucket + IAM Role + K8s Deployment を全部作る。

### 特徴

- **全クラウド対応** (AWS / GCP / Azure / Kubernetes / GitHub / etc.)
- Composition で深い抽象化が可能
- Pod が重い (1GB RAM+)、低リソース環境では負荷大
- Provider のバージョン管理 (Crossplane core + Provider) が複雑

## 比較表

| 観点 | Kro | Crossplane |
|---|---|---|
| 思想 | K8s ネイティブ RGD で複合リソースをバンドル | クラウド全体を K8s API 化 |
| 対象 | K8s リソース + (ACK 経由で) AWS リソース | AWS / GCP / Azure / その他全部 |
| 重さ | 軽量 (100m CPU) | 重 (1GB+ RAM) |
| API 成熟度 | alpha (v0.9.x) | Stable (v1.x) |
| 低リソース環境での運用 | ◎ | △ |
| 学習コスト | 中 (RGD 設計) | 高 (Composition + Provider) |
| Provider エコシステム | ACK 連携 (まだ少) | 豊富 (公式 + コミュニティ) |
| 規模 | 小〜中小 | 中小〜大規模 |
| 状態管理 | etcd 直接 | etcd 直接 |

## 選定の指針

判断基準: **軽量さ重視なら Kro、マルチクラウド/エコシステム重視なら Crossplane**。

Kro を選ぶ理由になりやすい点:

1. **リソース制約**: Crossplane core + Provider AWS で 1GB+ 消費します。RAM の限られた環境 (エッジ/SBC 等) では他 Pod の余裕が無くなる
2. **AWS 中心の構成**: GCP/Azure を使う予定がないなら、Crossplane の multi-cloud は overkill
3. **学習コスト**: Composition の設計は時間がかかる。RGD は K8s YAML の延長で書ける
4. **ACK との相性**: AWS リソース管理は ACK Controllers (IAM, S3) + Kro RGD でカバー可能

逆に以下の場合は Crossplane が向く:

- 複数クラウド (AWS + GCP) を統合管理
- 既に Crossplane に投資している
- Provider 経由で GitHub Repo / Slack Channel 等も管理したい

## Kro RGD の実装例: IRSA Role

EKS Hybrid Nodes 移行後、IRSA + ACK で IAM Role を K8s API で作る:

```yaml
apiVersion: kro.run/v1alpha1
kind: ResourceGraphDefinition
metadata:
  name: irsa-role
spec:
  schema:
    apiVersion: v1alpha1
    kind: IRSARole
    spec:
      roleName: string
      namespace: string
      serviceAccountName: string
      policies: "[]string"
  resources:
    - id: role
      template:
        apiVersion: iam.services.k8s.aws/v1alpha1
        kind: Role
        metadata:
          name: ${schema.spec.roleName}
        spec:
          name: ${schema.spec.roleName}
          assumeRolePolicyDocument: |
            { ... OIDC trust ... }
          policies: ${schema.spec.policies}
    - id: serviceaccount
      template:
        apiVersion: v1
        kind: ServiceAccount
        metadata:
          name: ${schema.spec.serviceAccountName}
          namespace: ${schema.spec.namespace}
          annotations:
            eks.amazonaws.com/role-arn: ${role.status.ackResourceMetadata.arn}
```

これを 1 つ書いておけば、各 Pod の IAM 構成は以下で済む:

```yaml
apiVersion: kro.run/v1alpha1
kind: IRSARole
metadata:
  name: prefect-s3-access
spec:
  roleName: prefect-s3-access
  namespace: prefect
  serviceAccountName: prefect
  policies:
    - arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

ACK IAM Controller が IAM Role を作り、ServiceAccount に annotation を付ける。Pod が IRSA で AWS API を叩ける。

## 採用判断フロー

```text
Q1. 小規模 / 低リソース環境 ?
  YES → Q2
  NO  → Crossplane

Q2. AWS 中心 ?
  YES → Kro (+ ACK)
  NO  → Crossplane

Q3. 複合リソースのバンドル抽象が欲しい ?
  YES → Kro RGD
  NO  → Kustomize で十分
```

## まとめ

- **Kro**: 軽量、K8s ネイティブ、低リソース 〜 中小規模に最適
- **Crossplane**: マルチクラウド、大規模、エコシステム成熟
- **AWS 中心 + EKS Hybrid Nodes** の構成では **Kro が有力**

両者は競合だが共存も可能。Kro が alpha のうちは破壊的変更に注意して使う。

## 次回予告

シリーズ最終回は **「EKS Hybrid Nodes で Cilium eBPF を読み解く」**。VPC CNI が使えない理由から、kube-proxy replacement のカーネルレベル動作、Pod-to-Pod パケットパスまで深く掘る。

## 参考

- [Kro 公式](https://kro.run/)
- [Kro GitHub](https://github.com/kro-run/kro)
- [Crossplane docs](https://docs.crossplane.io/)
- [ACK Controllers](https://aws-controllers-k8s.github.io/community/)
