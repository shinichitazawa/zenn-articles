---
title: "S3 はもはやサービス名ではなくプロトコルである"
emoji: "🪣"
type: "tech"
topics: ["s3", "aws", "objectstorage", "sakuracloud", "minio"]
published: false
---

## はじめに

さくらの高火力 DOK で生成 AI モデルを動かす検証中に、モデルキャッシュ置き場として**さくらのオブジェクトストレージ**(`https://s3.isk01.sakurastorage.jp`)を使うことになりました。エンドポイントに「s3」と入っているのに AWS は一切関係ない — この「S3 なのに AWS ではない」状況はいまや当たり前になっていますが、ではその「S3 互換」とは正確に何を指すのか。仕様書はあるのか、どこまで互換なら「互換」を名乗れるのか、壊れるとしたらどこから壊れるのか。本記事は S3 を**プロトコルとして**一次情報(AWS 公式ドキュメント、GitHub 上の実装と互換性テスト)で整理したものです。

- 想定読者: S3 互換ストレージ(さくら、Cloudflare R2、MinIO 等)を使う・選定する開発者
- 調査時点: 2026-08。リンク先の仕様・数値は変わり得ます

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が公式ドキュメント・GitHub リポジトリを参照して検証しています。誤りや改善点があれば、コメント等でご指摘ください。
:::

## 1. 正式な仕様書は存在しない

まず一番大事な事実から。**S3 プロトコルには IETF RFC のような中立の標準仕様が存在しません。** あるのは AWS の [Amazon S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html) だけで、これは「AWS のサービスのリファレンス」であって、互換実装のための契約書ではありません。

にもかかわらず S3 API は事実上の標準になりました。これは意見ではなく、**競合他社の公式ドキュメントが自ら「S3 互換」を謳っている**ことで確認できる事実です:

- Google Cloud Storage — [XML API の S3 互換運用(公式)](https://cloud.google.com/storage/docs/interoperability)
- Cloudflare R2 — [S3 API compatibility(公式)](https://developers.cloudflare.com/r2/api/s3/api/)
- Backblaze B2 — [S3 Compatible API(公式)](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api)
- さくらのオブジェクトストレージ — [Amazon S3 互換 API(公式マニュアル)](https://manual.sakura.ad.jp/cloud/objectstorage/about.html)

自社ネイティブ API を持つ Google までが S3 互換 API を併設している点に、この API の支配力が表れています。結果として「AWS が API を変えると、互換実装がそれを追いかける」という**片務的な標準化**が 20 年続いており、この構造が後述の 2025 年の互換性破壊を生みます。

## 2. プロトコルの解剖

### 2.1 基本形: リソース指向 REST

```
PUT    /{bucket}/{key}     オブジェクト作成
GET    /{bucket}/{key}     取得(Range 対応)
DELETE /{bucket}/{key}     削除
GET    /{bucket}?list-type=2   一覧(ListObjectsV2)
HEAD   /{bucket}/{key}     メタデータのみ
```

XML レスポンス(JSON ではなく、2006 年の設計のまま)と、`x-amz-*` 拡張ヘッダ群が特徴です。

### 2.2 認証: SigV4 — 互換実装の最初の関門

現行の認証は **AWS Signature Version 4**。リクエストを正規化(canonical request。メソッド・パス・クエリ・ヘッダ・ペイロードのハッシュを決められた順序と書式で 1 つの文字列に整形したもの)し、日付・リージョン・サービス名から導出した鍵で HMAC-SHA256 署名して `Authorization` ヘッダに載せます。互換ストレージを名乗るなら[この署名検証の実装が事実上必須](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html)で、旧 SigV2 のみ対応の実装は現代のクライアントから使えません。

さくらのオブジェクトストレージも SigV4 を実装しているからこそ、`aws` CLI・`s5cmd`・boto3・n8n の S3 ノードが `--endpoint-url` の差し替えだけで動きます。

### 2.3 アドレッシング: virtual-hosted vs path-style

```
virtual-hosted: https://{bucket}.s3.isk01.sakurastorage.jp/key
path-style:     https://s3.isk01.sakurastorage.jp/{bucket}/key
```

AWS は 2019 年に path-style の廃止を予告して大反発を受け、[既存バケットについては撤回](https://aws.amazon.com/blogs/aws/amazon-s3-path-deprecation-plan-the-rest-of-the-story/)しました(AWS 公式ブログ)。**互換ストレージでは path-style を使います**(ワイルドカード TLS 証明書が不要で、実装側の対応漏れが起きにくいため)。クライアント側では `force_path_style` 系の設定で明示できます。

### 2.4 その他の主要メカニズム

| 機構 | 概要 | 補足 |
|---|---|---|
| Multipart Upload | 分割並列アップロード。パート数上限 10,000・オブジェクト最大 5TB([公式仕様](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)) | 大容量転送の基盤。互換実装の品質差が出やすい |
| Presigned URL | 署名をクエリ文字列に埋めた時限 URL | 認証情報を渡さずに一時アクセスを許可 |
| 強整合性 | AWS S3 の read-after-write は強整合([公式: Consistency Model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel)。2020-12 の変更) | それ以前は結果整合。互換実装は各自の整合性モデルを持つ |
| 条件付き書き込み | `If-None-Match` での PUT(compare-and-swap 相当)。[2024-08 に AWS が追加](https://aws.amazon.com/about-aws/whats-new/2024/08/amazon-s3-conditional-writes/) | 新しめの API は互換実装が追いついていないことが多い |

## 3. GitHub で見る「S3 互換」の生態系

### 3.1 互換性テストという「事実上の適合試験」

中立仕様がない代わりに、コミュニティが作った互換性テストが適合試験の役割を果たしています。代表が **[ceph/s3-tests](https://github.com/ceph/s3-tests)** — Ceph プロジェクト発のテストスイートで、リポジトリ自身が「a set of **unofficial** Amazon AWS S3 compatibility tests」と明記しているとおり、これすら公式適合試験ではありません。boto3 ベースの数百のテストケースを実装に対して実行します。

対応する S3 API の範囲は実装ごとに大きく異なり(各実装が対応 API 一覧を公式に明記しています。例: [SeaweedFS の対応 API 表](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API))、s3-tests のパス状況もそれに応じて実装間で差が出ます(※パス数の横並び比較データは筆者未確認)。**「S3 互換」は二値ではなくグラデーション**です。採用判断では、「S3 互換」という表示ではなく「自分が使う API サブセットで s3-tests を実行した結果」で判断します。

### 3.2 主要なオープンソース実装

| 実装 | GitHub | 特徴 |
|---|---|---|
| [MinIO](https://github.com/minio/minio) | Go / AGPLv3 | 長らく互換実装の代名詞。近年は商用版への注力が進み、2025 年に Community Edition の Web 管理 UI が object browser 中心へ縮小された([object-browser#3509](https://github.com/minio/object-browser/pull/3509))。これを受けて代替を検討する議論も起きている([minio discussion #21320](https://github.com/minio/minio/discussions/21320)) |
| [Ceph RGW](https://github.com/ceph/ceph) | C++ / LGPL | s3-tests 本家。互換性は最も広いが運用は重量級 |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | Go / Apache-2.0 | 小さいファイル大量に強い設計。S3 API はサブセット |
| [Garage](https://git.deuxfleurs.fr/Deuxfleurs/garage) | Rust / AGPLv3 | 自宅・エッジ向けの軽量分散。地理分散前提の設計 |
| [LocalStack](https://github.com/localstack/localstack) | Python | テスト用エミュレータとしての S3 実装 |

商用の互換サービス(Cloudflare R2、Backblaze B2、Wasabi、**さくらのオブジェクトストレージ**等)もこの生態系の上にあり、どれも「AWS の API リファレンスを読み、s3-tests 的な検証で確からしさを担保する」という同じ方法で互換性を確保しています。

## 4. ケーススタディ: 2025 年 1 月、AWS SDK の更新で互換実装が一斉に動作不能になった

「片務的な標準」の脆さが露呈した最近の実例です。

- 2024-12: AWS が S3 の[デフォルトのデータ整合性保護](https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-s3-default-data-integrity-protections)を発表 — アップロード時に CRC32/CRC64NVME チェックサムを自動付与
- 2025-01: 各言語の AWS SDK がこれを**デフォルト有効**でリリース([aws-sdk-go-v2 の告知](https://github.com/aws/aws-sdk-go-v2/discussions/2960)等)
- 直後: `x-amz-checksum-crc32 ... not implemented` — チェックサム未実装の互換サービス(当時の Cloudflare R2、旧 MinIO、GCS の XML 互換 API など)への**アップロードが軒並み失敗**。[aws-sdk-go-v2 の公式ディスカッション](https://github.com/aws/aws-sdk-go-v2/discussions/2960)にも「サードパーティの S3 互換サービスでは失敗し得る」旨と回避設定が明記されています

回避策として SDK には `when_required` 設定(環境変数 `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` / `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`)が用意されました([AWS 公式: Data Integrity Protections](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html))。互換サービス各社はチェックサム対応を急ぐことになりました。

教訓は明確で、**「S3 互換」は静的な性質ではなく、AWS の変更に追従し続ける動的なプロセス**だということ。互換ストレージを使うシステムでは、AWS SDK のバージョンアップが「自分は AWS を使っていないのに」破壊的変更になり得ます。

## 5. 実務ガイド: 互換ストレージと付き合う設定

さくらのオブジェクトストレージ + `s5cmd`/boto3/n8n での実運用から:

```bash
# 1) エンドポイントを明示(これが「S3 互換」利用の本体)
s5cmd --endpoint-url https://s3.isk01.sakurastorage.jp cp s3://bucket/key ./local

# 2) SDK のチェックサム自動付与を「必要時のみ」に(2025 年問題の回避)
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

# 3) 互換ストレージでは path-style を明示
#    boto3: Config(s3={'addressing_style': 'path'})
```

選定時のチェックリスト:

1. **SigV4 対応か**(2026 年現在、非対応は選定対象外)
2. **使う API のサブセットが動くか** — 一覧・PUT/GET・multipart・presigned まで確認すれば大半のワークロードは足りる。s3-tests を自分で実行して確認する
3. **整合性モデル** — AWS の強整合を前提にしたコード(書いた直後に読む)が互換先でも成立するか
4. **新しめの API(条件付き書き込み、チェックサム等)への依存を避ける** — 互換実装が追いつくまでのタイムラグが常にある

## まとめ

- S3 に中立の標準仕様はない。AWS の API リファレンスが「仕様」で、互換実装がそれを追いかける片務的標準
- 認証の本体は SigV4。エンドポイント差し替え + path-style + SigV4 が「S3 互換」利用の 3 要素
- 互換性はグラデーション。適合試験に相当するものは [ceph/s3-tests](https://github.com/ceph/s3-tests)(それ自体 unofficial)しかなく、実装ごとの対応 API 差は各公式ドキュメントで確認するしかない
- 2025 年のチェックサム問題が示す通り、互換性は「維持し続ける作業」。SDK 更新は互換ストレージ利用者にとって破壊的変更になり得る
- ホスト名の「s3」はプロトコル名。`s3.isk01.sakurastorage.jp` は AWS と無関係のさくらのサービスであり、それでも aws CLI がそのまま使えるのがこの生態系の到達点

## 参考

一次情報(公式ドキュメント・公式リポジトリ):

- [Amazon S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html) / [SigV4 認証](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html) / [SDK Data Integrity Protections](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html)
- [AWS S3 default data integrity(2024-12 発表)](https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-s3-default-data-integrity-protections) / [aws-sdk-go-v2 の変更告知(公式リポジトリ)](https://github.com/aws/aws-sdk-go-v2/discussions/2960)
- [GCS Interoperability(S3 互換 XML API)](https://cloud.google.com/storage/docs/interoperability) / [Cloudflare R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/) / [Backblaze S3 Compatible API](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api)
- [ceph/s3-tests(unofficial 互換性テスト)](https://github.com/ceph/s3-tests)
- [MinIO](https://github.com/minio/minio) / [SeaweedFS](https://github.com/seaweedfs/seaweedfs) / [Garage](https://garagehq.deuxfleurs.fr/) / [LocalStack](https://github.com/localstack/localstack)
- [さくらのオブジェクトストレージ マニュアル](https://manual.sakura.ad.jp/cloud/objectstorage/about.html)
