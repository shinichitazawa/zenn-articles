---
title: "NetObserv でマルチクラウド k3s のノード間通信を観測する"
emoji: "🛰️"
type: "tech"
topics: ["kubernetes", "k3s", "ebpf", "keda", "azure"]
published: false
---

## はじめに

以前の記事（NetObserv eBPF Agent — CNI 非依存のネットワーク観測）では、NetObserv eBPF Agent の direct-flp モードを単一ノードで検証しました。本記事はその続編で、**オンプレの Raspberry Pi とクラウドのスポット VM（Azure・AWS）を 1 つの k3s クラスタに束ねた構成で、クラウドを跨ぐ pod-to-pod 通信を NetObserv が両側のノードから観測できること**を実機で確認します。GCP も試みましたが、後述する 2 つの制約（Cluster Autoscaler の GCE provider の実装と、e2-micro のリソース）により、ノードの join までで通信観測には至りませんでした。その経緯も含めて書きます。

もう 1 つのテーマは検証の仕方です。ノードやワークロードを手で起動してしまうと自動化の検証になりません。そこで **KEDA の cron スケーラが決まった時刻にワークロードを 0→1 に起こし、行き場のない Pod を Cluster Autoscaler が検知してクラウドノードを 0→1 で起動する**、という人手ゼロのチェーンを組み、その通信を NetObserv で観測しました。終了時刻には KEDA が 0 に戻し、Cluster Autoscaler がノードを畳むところまで自動です。

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が公式ドキュメントを引用して検証しています。誤りや改善点があれば、コメント等でご指摘ください。実測値・ログ・スクリーンショットは筆者環境で 2026-08-14 に取得したもので、読者環境では結果が異なる場合があります。
:::

## 検証環境（2026-08-14 時点）

| 項目 | 値 |
|---|---|
| control plane | Raspberry Pi 5（k3s `v1.36.2+k3s1`） |
| worker (Azure) | VMSS スポット（`Standard_B2pls_v2`、2 vCPU / 4 GiB、arm64、通常時 0 台） |
| worker (AWS) | ASG スポット（t4g 系、arm64、通常時 0 台） |
| worker (GCP) | MIG スポット（`e2-micro`、1 GiB、amd64、通常時 0 台）※join まで |
| ノード間接続 | Tailscale（各ノードが tailnet に参加し、その IP を k3s の node-ip に使用） |
| CNI | Cilium `v1.19.3`（kube-proxy replacement） |
| 観測 | netobserv-ebpf-agent（`:main` タグ、direct-flp モード） |
| スケール | KEDA（chart 2.20.1）+ Cluster Autoscaler（aws / azure / gce provider を各 1 プロセス） |

検証に使った構成ファイルは [k8s-deploy-public/netobserv](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/netobserv)（commit [`25777ef`](https://github.com/shinichitazawa/k8s-deploy-public/commit/25777ef) 時点。環境固有値はダミーに置換済み）にあります。

## 全体像

```mermaid
flowchart LR
  subgraph SCHED["自動チェーン"]
    K["KEDA cron<br/>11:50 に 0→1"] --> D["nginx Deployment<br/>(nodeSelector: cloud=azure)"]
    D -->|Pending| CA["Cluster Autoscaler"]
    CA -->|VMSS 0→1| VM["Azure スポット VM"]
  end
  subgraph OBS["観測"]
    A1["agent@rpi0"] --- P["Prometheus"]
    A2["agent@Azure"] --- P
  end
  C["client(常駐)@rpi0"] -->|HTTP| N["nginx@Azure"]
  VM -.->|join| N
  A1 -.観測.- C
  A2 -.観測.- N
```

client は rpi0 側に常駐させておき、nginx が現れた瞬間からクラウド跨ぎの通信になります。client を「置いておく」のは固定の土台であり、通信の開始はチェーンの完成そのものが引き金です。

## 人手ゼロで起こす — KEDA cron と Cluster Autoscaler

KEDA の [cron スケーラ](https://keda.sh/docs/2.20/scalers/cron/)は、時刻窓に入ると対象を望みのレプリカ数へスケールします。公式は次のように説明しています。

> When the time window starts, it will scale from the minimum number of replicas to the desired number of replicas based on your configuration.
>
> — [KEDA: Cron scaler](https://keda.sh/docs/2.20/scalers/cron/)（2026-08 時点で取得）

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: xcloud-nginx-cron, namespace: default}
spec:
  scaleTargetRef: {name: xcloud-nginx}
  minReplicaCount: 0
  maxReplicaCount: 1
  triggers:
    - type: cron
      metadata:
        timezone: Asia/Tokyo
        start: "50 11 * * *"
        end: "30 14 * * *"
        desiredReplicas: "1"
```

起こされた Pod は `nodeSelector: cloud=azure` を要求し、該当ノードが 0 台なので Pending になります。ここから先は前回記事（Cluster Autoscaler 編）で構築した scale-from-0 がそのまま働きます。VMSS に付けた node-template タグ（[Azure provider の規約](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/azure/README.md)で `/` を `_` に置換したもの）を読んで、Cluster Autoscaler が「このグループなら賄える」と判断します。

実際のイベントとタイムラインです（筆者環境の記録。JST）。

```text
--- Azure ---
11:50:13  KEDA cron が Active に遷移、nginx 0→1（Pending）
12:49:31  TriggeredScaleUp: pod triggered scale-up: [{azure-cil-vmss 0->1 (max: 2)}]
12:52:02  ノード Ready、nginx Running、client の応答が HTTP 200 に

--- AWS（別の KEDA 窓で同型を実行）---
13:35     KEDA cron が nginx-aws を 0→1（Pending）
13:38 頃  Cluster Autoscaler が ASG 0→1、EC2 スポット起動
13:41:59  client が AWS 側 nginx から HTTP 200

--- GCP（CA が使えないため代替経路。詳細は後述）---
14:38:15  CronJob が keyless(WIF) で MIG 0→1 に resize
14:42 頃  GCE VM が join（ただし e2-micro の資源不足で Ready を維持できず）
```

Azure の 11:50 と 12:49 の間が空いているのは、後述する落とし穴（包括 toleration と、詰まったインスタンスの後始末で発生させた CA の backoff）を踏んで修正していたためです。修正後のチェーン自体には人手は入っていません。なお AWS の検証中には、スポットの自然な入れ替わり（旧インスタンスの回収と新インスタンスの join）まで観測できました。

## NetObserv の配置 — direct-flp を全ノードへ

agent は [direct-flp モード](https://github.com/netobserv/netobserv-ebpf-agent/blob/main/docs/config.md)で動かします。公式の説明どおり、flowlogs-pipeline（FLP）が agent 内で動くため、collector を別に立てずに変換・出力まで完結します。

> In `direct-flp` mode, flowlogs-pipeline is run internally from the agent, allowing more filtering, transformations and exporting options.
>
> — [netobserv-ebpf-agent: config.md](https://github.com/netobserv/netobserv-ebpf-agent/blob/main/docs/config.md)（2026-08 時点で取得）

FLP の設定は `FLP_CONFIG` に渡します。今回の出力は 2 系統で、stdout（ログとして flow を確認する用）と Prometheus（ノード間の流量を可視化する用）です。

```json
{
  "pipeline": [
    {"name": "writer", "follows": "preset-ingester"},
    {"name": "prom", "follows": "preset-ingester"}
  ],
  "parameters": [
    {"name": "writer", "write": {"type": "stdout"}},
    {"name": "prom", "encode": {"type": "prom", "prom": {
      "prefix": "netobserv_",
      "metrics": [
        {"name": "node_flows_total", "type": "counter", "valueKey": "",
         "labels": ["SrcAddr", "DstAddr"], "filters": []}
      ]
    }}}
  ]
}
```

DaemonSet 側のポイントは 2 つです。

```yaml
spec:
  template:
    spec:
      tolerations:
        - operator: Exists          # CP とクラウドノードの dedicated taint 両方に載せる
      initContainers:
        - name: wait-cilium         # Cilium の healthz(:9879) を待ってから起動する
          image: busybox:1.36
          command: ["sh","-c","until wget -q -T 3 -O /dev/null http://127.0.0.1:9879/healthz; do sleep 10; done"]
```

DaemonSet 側の包括 toleration は全ノード配置のためのもので問題ありませんが、**通常の Pod に同じ toleration を付けてはいけません**（後述の落とし穴 1）。initContainer は、後述の落とし穴 3 で導入した安全策です。

## 観測結果 — 同じ通信を両側から見る

client（rpi0 上、Pod IP `10.0.0.8`）から nginx（Azure 上、Pod IP `10.0.3.150`）への HTTP を、両ノードの agent がそれぞれ観測しました。

Azure 側 agent（要求の到着。nginx の veth `lxcfc89...` で観測）:

```text
map[AgentIP:10.123.1.4 Bytes:572 DstAddr:10.0.3.150 DstPort:80 Interfaces:[lxcfc89467d8c93]
    Packets:7 Proto:6 SrcAddr:10.0.0.8 ...]
```

rpi0 側 agent（応答の到着。client の veth `lxc41cb...` で観測）:

```text
map[AgentIP:192.0.2.17 Bytes:1191 DstAddr:10.0.0.8 DstPort:47740 Interfaces:[lxc41cbf1c5da6e]
    Packets:5 Proto:6 SrcAddr:10.0.3.150 ...]
```

同一のクラウド跨ぎ通信について、要求側と応答側が別ノードの agent に現れています。CNI は Cilium のままで、NetObserv は CNI に依存せず TC(tcx) フックで観測しているため、この構成でもそのまま動きます。

AWS でも同じ形の両側観測が取れました。累計カウンタを見ると、client(`10.0.0.102`) と AWS 側 nginx(`10.0.2.97`) のペアが**双方向 × 2 系列ずつ**（rpi0 の agent と EC2 の agent がそれぞれ独立に数えたもの）出ています。

```text
10.0.0.102 -> 10.0.2.97 = 654   # rpi0 側 agent の観測
10.0.0.102 -> 10.0.2.97 = 706   # EC2 側 agent の観測
10.0.2.97 -> 10.0.0.102 = 651 / 661（同上の逆方向）
```

Prometheus 側では、FLP が出す `netobserv_node_flows_total` を既存の prometheus-server が annotation 経由で scrape します。3 時間分のグラフには検証の経過がそのまま残りました。12:51 に Azure ペアが立ち上がり、13:35 過ぎに client Pod の入れ替えと Azure ノードの一時的なハートビート断で系列が切り替わり、以降は AWS ペアが（スポットの入れ替わりによる Pod IP の変化も含めて）続きます。

![netobserv_node_flows_total の 3 時間。Azure 期(03:51-04:40 UTC)→ client 入替の断絶 → AWS 期(04:45-)。スポット入替で系列が変わる様子も見える](/images/026-netobserv-prom-flows.png)

![Prometheus の targets。両ノードの agent が UP](/images/026-netobserv-prom-targets.png)

## GCP だけ Cluster Autoscaler が使えなかった

AWS と Azure が同じ形で成立した一方、GCP では Cluster Autoscaler の GCE provider が**混在 providerID クラスタで scale-up を実行できない**ことが分かりました。GCE provider は判断材料の棚卸しでクラスタの全ノードの providerID を `gce://` として解釈しようとし、他形式に出会うと「無視」ではなくエラーを返すためです。バージョンを変えて 3 回試しましたが、エラー箇所が移動するだけでした（筆者環境の実測）。

```text
v1.32 / v1.34:
  Failed to get node infos for groups: wrong id: expected format
  gce://<project-id>/<zone>/<name>, got k3s://raspberrypi-0
v1.35:
  (棚卸しは通過するが scale-up 直前で)
  Failed to scale up: could not create quotas tracker: failed to get
  node group for node "raspberrypi-0": wrong id: expected format gce://...
```

AWS / Azure の provider は自形式でない providerID を単に読み飛ばすため、同じクラスタで問題なく動きます。この差は実装依存で、`k3s://` の control plane や他クラウドの worker が同居する self-hosted 構成では、GCE provider は現状使えないという結論になりました。

なお調査の過程で、GCE provider の scale-from-0 が**ノードの label/taint を instance template のメタデータ `kube-env`（`AUTOSCALER_ENV_VARS`）から読む**ことも確認し、template には `node_labels=cloud=gcp,...` を追加済みです（CA が解析するところまでは動きました）。GKE 以外でこの経路を使う場合の必須設定ですが、上記の制約により今回は活きませんでした。

代替として、CA-gcp 用に構築済みだった keyless（Workload Identity Federation）の配線をそのまま流用し、**CronJob が STS token-exchange → サービスアカウント impersonation → Compute API で MIG を resize** する時刻ベースの自動スケールを組みました。これは動作し、14:38 の自動発火で GCE VM が起動して k3s に join、Cilium も起動しました。ただし `e2-micro`（1 GiB・共有 vCPU）では kubelet がリソース飢餓で Ready を維持できず、ワークロード配置と flow 観測には至りませんでした。落とし穴 3 と同根で、これが 2 つ目のクラウドでの再現です。

## 踏んだ落とし穴

### 1. 包括 toleration の Pod が「死にかけノード」に吸着する

検証用 Pod に `tolerations: [{operator: Exists}]` を付けていたところ、**cordon（`node.kubernetes.io/unschedulable`）や NotReady の taint まで許容してしまい**、撤収中の死にかけノードへスケジュールされました。Pod は Pending にならないため、Cluster Autoscaler は「unschedulable な Pod なし」と判断してスケールアップしません。toleration は対象クラウドの dedicated taint だけに絞る必要があります（DaemonSet の全ノード配置とは要件が異なります）。

### 2. FLP の Prometheus は write ではなく encode、ポートは既定の :9090

FLP の設定で Prometheus を `write` ステージに書くと、起動時に panic します（`getWriter` で落ちる様子がスタックトレースに出ます）。正しくは `encode` ステージです。また設定に port を書いても実測では効かず、メトリクスサーバは既定の `:9090` で待ち受けました（起動ログに `StartServerAsync: addr = :9090` と出ます）。scrape 側の annotation はこの実効ポートに合わせます。

### 3. 1 GiB の VM では Cilium が起動ループに入る

当初の VMSS は `Standard_B2pts_v2`（2 vCPU / 1 GiB）でした。ブート直後は k3s agent・Cilium・Envoy・Tailscale・NetObserv agent の初期化が重なり、**load average が 2 vCPU に対して 10〜11 のまま 28 分収束しませんでした**（実測）。Cilium は probe が応答できず、`Timed out waiting for pre-existing resources ... CiliumNetworkPolicy` の fatal で再起動し、再起動のたびに BPF の再ロードで負荷が上がる悪循環に入ります。`Standard_B2pls_v2`（4 GiB）へ上げたところ、同じ構成が数分で安定しました。あわせて、NetObserv agent には Cilium の healthz を待つ initContainer を入れ、ブート時の競合を減らしています。なお GCP の `e2-micro`（1 GiB・共有 vCPU）でも同根の症状（kubelet が Ready を維持できない）を確認しており、この構成のノードは実質 2 vCPU / 4 GiB が下限と考えています。

### 4. ASG のタグが消えると CA は静かに沈黙する

AWS 側が最初うんともすんとも言わなかった原因は、ASG から Cluster Autoscaler 用のタグが消えていたことでした（`Name` タグ 1 つだけが残った状態）。auto-discovery タグ 2 つ（`k8s.io/cluster-autoscaler/enabled` とクラスタ名）が無いと CA はグループを発見せず、scale-from-0 用の node-template タグ 3 つ（label 2 + taint 1）が無いと発見しても賄えると判断できません。どちらもエラーにはならず、単に何も起きないため気づきにくいです。5 つのタグを付け直したところ、その周回から発見・スケールとも正常になりました。

### 5. IP をラベルにした flow メトリクスは Pod の入れ替えで分断される

flow メトリクスを `SrcAddr`/`DstAddr` ラベルで集計していたため、client Pod をローリング更新した瞬間に IP が変わり、**グラフ上は通信が止まったように見えました**（実際は新 IP の別系列として継続）。スポットの入れ替わりでも同じことが起きます。K8s 名（Pod 名や workload 名）で追いたい場合は、FLP の Kubernetes enrichment を有効にしてラベルを付け替える必要があります。direct-flp の素の flow は IP の世界だ、という当たり前の事実を、グラフの「偽の断絶」で体感しました。

### 6. 外部からインスタンスを消すと Cluster Autoscaler が backoff する

詰まったインスタンスを `az vmss delete-instances` で外から消したところ、ちょうど走っていた Cluster Autoscaler のリサイズ要求と競合して失敗が記録され、**ノードグループがスケールアップ backoff に入りました**。イベントには何も出ず、ログに `Node group azure-cil-vmss is not ready for scaleup - backoff` が出るだけなので気づきにくいです。CA が管理するリソースには外から触らないのが原則で、触ってしまった場合は backoff の解消（時間経過か CA の再起動）が要ります。

## まとめ

- NetObserv eBPF Agent（direct-flp）は、Tailscale 越しに束ねたマルチクラウド k3s で、**Azure と AWS の両方についてクラウドを跨ぐ pod-to-pod 通信を両側のノードから**観測できました。
- 検証チェーンは KEDA cron（0→1）と Cluster Autoscaler（node group 0→1）で人手ゼロにでき、終了時刻の自動撤収まで含めて再現可能です。スポットの自然な入れ替わりもそのまま観測に乗りました。
- GCP は Cluster Autoscaler の GCE provider が混在 providerID クラスタで scale-up できないため（3 バージョンで実測）、keyless の CronJob resize で代替しました。VM の join までは自動で成立しましたが、e2-micro の資源不足で通信観測には至っていません。
- 実測で 6 つの落とし穴を踏みました。特に「包括 toleration が死にかけノードに吸着して CA が発火しない」「1 GiB 級 VM では k3s+Cilium が安定しない（Azure/GCP の 2 クラウドで再現）」「IP ラベルの flow メトリクスは Pod 入れ替えで分断される」は、同種の構成を組む際に先に知っておくと時間を節約できます。

## 参考

- 検証時の構成ファイル: [netobserv（overlay と検証フィクスチャ）](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/netobserv)（[k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public) commit [`25777ef`](https://github.com/shinichitazawa/k8s-deploy-public/commit/25777ef) 時点。環境固有値はダミーに置換済み）
- [netobserv-ebpf-agent: configuration（EXPORT / direct-flp / FLP_CONFIG）](https://github.com/netobserv/netobserv-ebpf-agent/blob/main/docs/config.md)
- [flowlogs-pipeline](https://github.com/netobserv/flowlogs-pipeline)
- [KEDA: Cron scaler](https://keda.sh/docs/2.20/scalers/cron/)
- [Cluster Autoscaler: Azure provider（node-template タグ規約）](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/azure/README.md) / [AWS provider（auto-discovery と node-template タグ）](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/aws/README.md) / [GCE provider](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler/cloudprovider/gce)
- [Cilium: kube-proxy replacement](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/)
