---
title: "33B 動画生成モデルを動かす GPU を借りるか買うか(2026-08 実測)"
emoji: "🎛️"
type: "tech"
topics: ["gpu", "cloud", "ai", "sakuracloud", "gcp"]
published: false
---

## はじめに

オープンウェイトの動画生成モデル MiniMax H3(33B、量子化済みで重み約 44.5GB)を自前で動かそうとして、1 週間で Google Colab・GCP・さくらの高火力 DOK・GPU 購入(BTO(受注生産 PC)/中古)を横断的に検討・実測しました(後半のコスト比較では、参考として市場型 GPU クラウドの Vast.ai・RunPod と AWS Spot も同じ表に並べます)。本記事はその過程で取れた一次データ — **無料 GPU が OOM で停止する瞬間のログ、Spot GPU がクォータで拒否される正確なエラー、秒課金 GPU の実測価格** — を整理し、「クラウドで借りる vs 買う」の判断材料にまとめたものです。

- 想定読者: ローカル LLM / 画像・動画生成を自前で動かしたい個人〜小規模チーム
- 題材ワークロード: ComfyUI + MiniMax H3(VRAM 16GB+ / システム RAM 32GB+ が目安。根拠は後述)
- 価格・在庫は 2026-08-29 時点の実測または公式ページの値です。スポット価格と中古相場は変動します

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が実測ログまたは公式ドキュメントで検証しています。誤りや改善点があれば、コメント等でご指摘ください。
:::

## 前提: ワークロードが要求を決める

比較の前に「何を動かすか」を固定します。H3 の公式チェックポイント([Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3))の実サイズ:

| ファイル | サイズ |
|---|---|
| diffusion (pruned int8) | 21.0 GB |
| text encoder (NVFP4) | 15.7 GB |
| video/audio VAE | 5.8 GB |
| turbo LoRA | 2.0 GB |
| **T2V 最小セット合計** | **約 44.5 GB** |

※NVFP4 は NVIDIA の 4bit 浮動小数点形式。世代ごとの対応は後述の GPU 比較表の節で扱います。

VRAM に全部は載らないため、ComfyUI は重みをストリーミングします(後述の実測どおり、これは実際に機能します)。**効いてくるのは VRAM 単体ではなく「VRAM + システム RAM + ディスク」の 3 段構え**で、ここが安い GPU インスタンス選びで見落としやすい注意点になります。

## 実測 1: 無料 Colab (T4) は VRAM ではなく RAM で停止する

Colab 無料枠の T4(VRAM 16GB / システム RAM 12GB)に ComfyUI + H3 を載せた実測:

```text
[INFO] Model MiniMaxH3TEModel_ prepared for dynamic VRAM loading.
       14956MB Staged. 0 patches attached.
```

15.7GB のテキストエンコーダは **dynamic VRAM loading で 16GB の T4 にステージできた**(NVFP4 の混合精度も Turing 世代で動作)。ところがその直後、システム RAM 12GB が枯渇して OOM killer(Out Of Memory killer。メモリ不足時に Linux カーネルがプロセスを強制終了する仕組み)がプロセスを終了させ、セッションごと消滅しました。停止直前の状態:

```text
GPU: 8255 MiB used
RAM: total 12GB / used 9GB / available 2GB / swap 0
```

**重み 44.5GB をストリーミングするには、あふれた分を受けるシステム RAM が要ります。** VRAM のスペック表だけ見て借りると、この形で失敗します。逆に言えば VRAM 16GB 級でも RAM が 32GB+ あれば動作が見込めます — これが以降の選定基準「VRAM 16GB+ / RAM 32GB+」の根拠です(実測で成功した構成は RAM 40GB(DOK)と 53GB(Colab Pro)で、失敗した構成は 12GB。32GB は「重み 44.5GB − VRAM 16GB ≒ 28.5GB を RAM 側で受けられる」ことから置いた下限の見積りです)。

## 実測 2: GCP の Spot GPU は「グローバルクォータ」に阻まれる

GCP の g2-standard-16(L4 24GB / RAM 64GB、Spot で ¥55〜70/時)は要件を満たす有力候補でしたが、新規プロジェクトでは 2 段のクォータがあります。

- リージョン別 `PREEMPTIBLE_NVIDIA_L4_GPUS`: **主要リージョンで既定 1 が付与済み**(実測)
- グローバル `GPUS_ALL_REGIONS`: **既定 0**

「Spot はグローバル枠の対象外ではないか」という想定は、実測で否定されました。在庫のある us-central1-a で Spot 作成を試すと:

```text
ERROR: Quota 'GPUS_ALL_REGIONS' exceeded.  Limit: 0.0 globally.
       metric name = compute.googleapis.com/gpus_all_regions
```

**`GPUS_ALL_REGIONS` は Spot にも適用されます。** 紛らわしいのは、在庫切れ(stockout)のリージョンではクォータ判定に到達する前に stockout エラーが返ること。東京で stockout、アイオワでクォータ拒否、という順で見ると「東京はクォータを通った」と誤読しがちですが、単に判定順の違いです。

なお API 経由の自動クォータ申請(0→1)は denied で返りました。コンソールから理由を書いて申請するルートが残っていますが、「今日動かす」には使えません。

## 実測 3: さくら高火力 DOK — クォータ不要・秒課金の V100

[高火力 DOK](https://www.sakura.ad.jp/koukaryoku-dok/) はコンテナ型 GPU クラウドで、Docker イメージを投げて実行秒数だけ課金されます。

| プラン | GPU / RAM | 料金(公式、2026-08-29) |
|---|---|---|
| V100 | V100 32GB / RAM 40GB / 3vCPU | **0.016 円/秒(57.6 円/時)** |
| H100 | H100 80GB / RAM 192GB / 10vCPU | 0.28 円/秒(1,008 円/時) |

- クォータ申請不要。既存のさくらアカウントで使える(初回のみ利用規約への同意が必要 — API からの投入も同意前は `403 Agreement to terms of service is required` で拒否されます。実測)
- Docker Hub の公開イメージを直接指定可能。成果物は `/opt/artifact` に置くと回収できる
- API はさくらのクラウドの API キーで Basic 認証。ただし**キーに DOK の操作権限が必要**(最小権限キーだと 403。実測)

V100 32GB + RAM 40GB は上の要件を満たします。

**実走結果(2026-08-29)**: DOK の V100 プランで H3 の T2V を 1 本生成できました。

- タスク実行 790 秒(13.2 分)= 重み 44.5GB のダウンロード約 8 分 + モデル初期化 3.2 分 + サンプリング 4.4 秒/step × 4 steps(turbo LoRA)
- 出力: 608×352 / 1.6 秒 / h264 + **ステレオ AAC**(H3 は映像と音声を単一パスで同時生成する。[公式 model card](https://huggingface.co/MiniMaxAI/MiniMax-H3) が「The H3-Omni-Transformer jointly predicts video and audio latents, which are then decoded into video and stereo audio, respectively.」と説明しています(2026-09 取得))
- **実費: 約 12.6 円**
- 懸念だった Volta 世代の制約は、ComfyUI(comfy_kitchen)側が吸収: ログに
  `Native ops: convrot_w4a4, int8_tensorwise, ... emulated ops: nvfp4, float8_*` とあり、
  **NVFP4 の text encoder はエミュレーション実行にフォールバックして動作**しました
- 重みはタスクごとに消える(約款どおりデータ非保存)ため、毎回 8 分の DL が走る。
  量産するなら 1 タスクで複数クリップ生成するのが効率的

## 性能比較: 「何相当の GPU を借りているのか」

クラウドでよく出てくる GPU と消費者向けカードを同じ表に並べます(公称値ベース、丸めあり)。

| GPU | 世代 | VRAM | 帯域 | FP16 Tensor | bf16 | fp8/fp4 | 参考価格 |
|---|---|---|---|---|---|---|---|
| T4 | Turing 2018 | 16GB | 320 GB/s | 65 TF | ✗ | ✗ | クラウド専用 |
| **V100 32GB** | Volta 2017 | 32GB | **900 GB/s** | 112〜125 TF | ✗ | ✗ | 中古 ¥15〜25万(非推奨) |
| L4 | Ada 2023 | 24GB | 300 GB/s | 121 TF | ✓ | fp8 | クラウド専用 |
| A100 80GB | Ampere 2020 | 80GB | 2.0 TB/s | 312 TF (bf16) | ✓ | ✗ | 中古でも数百万 |
| H100 80GB | Hopper 2022 | 80GB | 3.35 TB/s | 990 TF (bf16) | ✓ | fp8 | 同上 |
| RTX 3090(中古) | Ampere 2020 | 24GB | 936 GB/s | 71〜142 TF | ✓ | ✗ | **¥12〜15万** |
| RTX 4090(中古) | Ada 2022 | 24GB | 1.0 TB/s | 165〜330 TF | ✓ | fp8 | 約 ¥40万(高騰中) |
| RTX 5060 Ti 16GB | Blackwell 2025 | 16GB | 448 GB/s | — | ✓ | fp4 | 新品 約 ¥9万 |
| RTX 5090 | Blackwell 2025 | 32GB | 1.79 TB/s | — | ✓ | fp4 | 新品 単体 ¥90万・BTO 一式 ¥75〜113万 |

スペック列は各製品の NVIDIA 公式データシート([データセンター GPU](https://www.nvidia.com/en-us/data-center/products/)、[GeForce](https://www.nvidia.com/ja-jp/geforce/graphics-cards/))の公称値を丸めたものです。表中の TF は TeraFLOPS(1 秒あたり 1 兆回の浮動小数点演算)です。**「参考価格」列の中古・BTO 実勢価格は 2026-08 時点の筆者調べで、一次資料が存在しない数値です**(中古相場・品薄プレミアムは日々変動します。購入時は販売店の現在価格を確認してください)。

読み方のポイント:

1. **DOK の V100 は「帯域と VRAM は RTX 3090 以上、演算は 3090 前後、ソフト対応は終盤」**。生成系ワークロードは帯域律速になりやすいので、900 GB/s は数字以上に効きます
2. **性能表に出ない軸 = 量子化フォーマットの対応世代**。H3 のテキストエンコーダは NVFP4(NVIDIA の 4bit 形式)で、ネイティブ実行は Blackwell 世代前提。旧世代では逆量子化フォールバック(4bit の重みを実行前に fp16 などへ戻して計算する方式)の可否がモデル実装依存になります。今回は ComfyUI 側がこのフォールバックを実装していたため、Turing(T4)でも Volta(V100)でも NVFP4 のテキストエンコーダが動きました(実測)。Volta は bf16 も持たないため、bf16 前提のモデルは fp16 フォールバック確認が必要
3. **CUDA のサポート打ち切り**も実務では性能より先に効きます。CUDA 13 系は Volta をサポート対象から外したため([CUDA Toolkit リリースノート](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html)では cuFFT / cuSPARSE などが 13.0 で Maxwell・Pascal・Volta(Turing より前の compute capability)のサポート削除を明記。2026-09 取得)、V100 で動かすにはベースイメージを CUDA 12 系に固定する必要があります(PyTorch 公式イメージなら `*-cuda12.x-*` を明示)
4. RTX 5090 はカード単体(約 ¥90 万)より **BTO 一式(最安 ¥75 万前後)が安い逆転**が起きています(2026-08 時点の品薄相場)

## コスト比較: 借りる vs 買う

時間単価(2026-08-29 実測・目安):

| 調達先 | 構成 | 円/時 | クォータ | 即時性 |
|---|---|---|---|---|
| **さくら DOK** | V100 32GB / RAM 40GB | **57.6** | 不要 | 規約同意のみ |
| Vast.ai | 4060 Ti 16GB〜 | 9〜 | 不要 | 新規アカウント要 |
| RunPod | 3090 / A5000 24GB | 24〜33 | 不要 | 新規アカウント要 |
| GCP Spot | L4 24GB / RAM 64GB | 55〜70 | **要(通るまで使えず)** | ✗ |
| AWS Spot | g6.2xlarge (L4 24GB) | 50〜75 | 要 | ✗ |
| Colab Pro | L4 24GB / RAM 53GB | 実効 30 前後(月 ¥1,179) | 不要 | 課金即時 |
| 電気代(参考) | 消費 300〜575W | 9〜18 | — | — |

購入との損益分岐(概算。電気代 ¥11/時を購入側コストに計上):

| 購入 | 一式費用 | DOK(57.6円/時)比の分岐点 | 分岐までの使用量 |
|---|---|---|---|
| 中古 RTX 3090 24GB | 約 ¥15万(+筐体があれば) | ≈ 3,200 時間 | 毎日 3 時間 × 3 年 |
| RTX 5060 Ti 16GB 新品 + 筐体 | 約 ¥20万 | ≈ 4,300 時間 | 毎日 3 時間 × 4 年 |
| RTX 5090 BTO | ¥75〜113万 | ≈ 16,000 時間〜 | 毎日 8 時間 × 5.5 年 |

購入側の金額は前節と同じく筆者調べの実勢(2026-08 時点、一次資料なし)、電気代は 300〜575W × ¥31/kWh 前後での概算です。

数字の上では、**毎日回す実績がつくまでは秒課金・時間課金で借りる方が有利**です。特に検証フェーズ(数十時間)なら、DOK の 57.6 円/時は総額数千円で終わります。

## 使い分けの指針

1. **品質・速度の検証(単発ジョブ)** → さくら DOK。クォータなし・秒課金・イメージを投げるだけ。Volta 由来の注意(CUDA 12 固定・bf16 なし)だけ踏まえる
2. **対話的に試行錯誤したい** → Colab Pro(L4 24GB / RAM 53GB)。無料枠 T4 は RAM 12GB が今回級のモデルでは足りない(実測)
3. **k8s クラスタに GPU ノードとして編入したい** → GCP/AWS の Spot VM(DOK と Colab はジョブ/ノートブック型のため不可)。ただし**クォータ申請のリードタイムを工程に織り込む**こと。GCP は `GPUS_ALL_REGIONS`(Spot にも適用)、AWS は G/VT インスタンスの Spot vCPU 枠
4. **購入** → 稼働時間の実績が出てから。買うなら現時点の候補は中古 RTX 3090 24GB(¥12〜15万、マイニング酷使個体を避けて保証付き店頭で)か、省電力の RTX 5060 Ti 16GB。RTX 5090 は BTO 一式が単体より安い相場の間は BTO で

## 補足: モデルライセンスも「調達」のうち

H3 の重みは [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE) で配布されています(2026-08-29 確認)。生成物に MiniMax は権利を主張せず、商用利用も年商 2,000 万ドルまでは自由ですが、**適用地域から EU・英国・韓国・米国が除外されている**点と、商用製品での「MiniMax H3」表示義務は珍しい条項です。GPU の調達先を選ぶ前に、動かすモデルのライセンスが自分の地域・用途で成立するかの確認を(「オープンウェイト ≠ オープンライセンス」)。

## まとめ

- 重み 44.5GB 級のモデルは「VRAM + RAM + ディスク」の 3 段で考える。無料 Colab で失敗した主因は VRAM ではなく RAM 12GB(実測)
- GCP のグローバル GPU クォータは **Spot にも適用される**(実測)。「今日動かしたい」に GCP/AWS は向かない
- さくら高火力 DOK は「クォータなし・57.6 円/時・秒課金」で検証用途の実用最安。ただし V100=Volta の世代制約(bf16 なし・CUDA 12 固定)に注意
- 購入の損益分岐は中古 3090 でも約 3,200 時間。毎日回す実績がつくまでは借りる

## 参考

- [高火力 DOK](https://www.sakura.ad.jp/koukaryoku-dok/) / [DOK API ドキュメント](https://manual.sakura.ad.jp/koukaryoku-dok-api/spec.html)
- [GCP GPU クォータ(resource-usage)](https://cloud.google.com/compute/resource-usage)
- [Comfy-Org/MiniMax-H3(モデル実サイズ)](https://huggingface.co/Comfy-Org/MiniMax-H3)
- [MiniMax H3 in ComfyUI(公式チュートリアル)](https://docs.comfy.org/tutorials/video/minimax/minimax-h3)
- [Colab 料金プラン](https://colab.research.google.com/signup)
