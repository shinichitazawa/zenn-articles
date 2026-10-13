---
title: "「S3 互換」は何が互換なのか — AWS が決める API 標準と 2025 年の互換性破壊"
emoji: "🪣"
type: "tech"
topics: ["s3", "aws", "objectstorage", "sakuracloud", "minio"]
published: false
---

## はじめに

さくらの高火力 DOK で生成 AI モデルを動かす検証中に、モデルキャッシュ置き場として**さくらのオブジェクトストレージ**(`https://s3.isk01.sakurastorage.jp`)を使うことになりました。エンドポイントに「s3」と入っていますが、AWS のサービスではありません。このように AWS 以外の「S3 互換」ストレージは広く使われていますが、その「互換」が具体的に何を指すのかを、筆者は説明できませんでした。仕様書は存在するのか、どの範囲の API が動けば「互換」と言えるのか、互換性が失われるのはどのような場合か。本記事はこれらを、 S3 を**プロトコルとして**一次情報(AWS 公式ドキュメント、GitHub 上の実装と互換性テスト)で整理したものです。ここで言うプロトコルは、TCP や HTTP のような通信方式ではなく、HTTP の上に乗った API の取り決め(URL の形、ヘッダー、署名、応答の形式)を指します。

- 想定読者: S3 互換ストレージ(さくら、Cloudflare R2、MinIO 等)を使う・選定する開発者
- 調査時点: 2026-08。リンク先の仕様・数値は変わり得ます

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が公式ドキュメント・GitHub リポジトリを参照して検証しています。誤りや改善点があれば、コメント等でご指摘ください。
:::

## 1. 仕様書の有無

S3 の API は、AWS 自身が詳しく公開しています。人が読む形の [Amazon S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html) に加えて、2025 年 6 月からは全サービスの API 定義を機械可読の形式で公開しています。

> AWS announces an official source for AWS API Model definition files and service model packages, providing developers with access to API definitions for all AWS services. We now publish daily updates of these API models to an open-source GitHub repository in Smithy format and also publish these packages to Maven Central.
>
> — [AWS announces open-source AWS API Models(2025-06-13)](https://aws.amazon.com/about-aws/whats-new/2025/06/open-source-aws-api-models)、2026-10 時点で取得

つまり、仕様書に当たるものはあります。ないのは、**中立の標準としての仕組み**です。

| 項目 | S3 の場合 |
|---|---|
| 誰が決めるか | AWS が単独で決める。IETF RFC のような、標準化団体で合意する手続きはない |
| 変更 | AWS の判断で変わる(§4 の 2025 年の事例) |
| 互換実装への約束 | ない。公開されている定義は AWS 自身のサービスの説明で、他社の実装との互換性を保証するものではない |
| 「S3 互換」の認定 | ない。互換を名乗るのは各社の自己申告(§3.1) |

にもかかわらず S3 API は事実上の標準になりました。このことは、**競合他社の公式ドキュメントが自ら「S3 互換」を謳っている**ことから確認できます:

- Google Cloud Storage — [XML API の S3 互換運用(公式)](https://cloud.google.com/storage/docs/interoperability)
- Cloudflare R2 — [S3 API compatibility(公式)](https://developers.cloudflare.com/r2/api/s3/api/)
- Backblaze B2 — [S3 Compatible API(公式)](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api)
- さくらのオブジェクトストレージ — [Amazon S3 互換 API(公式マニュアル)](https://manual.sakura.ad.jp/cloud/objectstorage/about.html)

自社ネイティブ API を持つ Google までが S3 互換 API を併設している点に、この API の影響力の大きさが表れています。結果として「AWS が API を変えると、互換実装がそれを追いかける」という**片務的な標準化**が 20 年続いており、この構造が後述の 2025 年の互換性破壊を生みます。

## 2. プロトコルの解剖

### 2.1 基本形: リソース指向 REST

```text
PUT    /{bucket}/{key}     オブジェクト作成
GET    /{bucket}/{key}     取得(Range 対応)
DELETE /{bucket}/{key}     削除
GET    /{bucket}?list-type=2   一覧(ListObjectsV2)
HEAD   /{bucket}/{key}     メタデータのみ
```

XML レスポンス(JSON ではなく、2006 年の設計のまま)と、`x-amz-*` 拡張ヘッダ群が特徴です。

### 2.2 認証: SigV4 — 互換実装の前提条件

現行の認証は **AWS Signature Version 4**。リクエストを正規化(canonical request。メソッド・パス・クエリ・ヘッダ・ペイロードのハッシュを決められた順序と書式で 1 つの文字列に整形したもの)し、日付・リージョン・サービス名から導出した鍵で HMAC-SHA256 署名して `Authorization` ヘッダに載せます。互換ストレージを名乗るなら[この署名検証の実装が事実上必須](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html)で、旧 SigV2 のみ対応の実装は現代のクライアントから使えません。

さくらのオブジェクトストレージも SigV4 を実装しているからこそ、`aws` CLI・`s5cmd`・boto3・n8n の S3 ノードが `--endpoint-url` の差し替えだけで動きます。

### 2.3 アドレッシング: virtual-hosted vs path-style

```text
virtual-hosted: https://{bucket}.s3.isk01.sakurastorage.jp/key
path-style:     https://s3.isk01.sakurastorage.jp/{bucket}/key
```

AWS は 2019 年に path-style の廃止を予告して利用者からの反対を受け、[既存バケットについては撤回](https://aws.amazon.com/blogs/aws/amazon-s3-path-deprecation-plan-the-rest-of-the-story/)しました(AWS 公式ブログ)。**互換ストレージでは path-style を使います**(ワイルドカード TLS 証明書が不要で、実装側の対応漏れが起きにくいため)。クライアント側では `force_path_style` 系の設定で明示できます。

### 2.4 その他の主要メカニズム

| 機構 | 概要 | 補足 |
|---|---|---|
| Multipart Upload | 分割並列アップロード。パート数上限 10,000・オブジェクト最大 5TB([公式仕様](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)) | 大容量転送の基盤。互換実装の品質差が出やすい |
| Presigned URL | 署名をクエリ文字列に埋めた時限 URL | 認証情報を渡さずに一時アクセスを許可 |
| 強整合性 | AWS S3 の read-after-write は強整合([公式: Consistency Model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel)。2020-12 の変更) | それ以前は結果整合。互換実装は各自の整合性モデルを持つ |
| 条件付き書き込み | `If-None-Match` での PUT(compare-and-swap 相当)。[2024-08 に AWS が追加](https://aws.amazon.com/about-aws/whats-new/2024/08/amazon-s3-conditional-writes/) | 新しめの API は互換実装が追いついていないことが多い |

## 3. GitHub で見る「S3 互換」の生態系

### 3.1 公式の適合試験はなく、非公式の互換性テストが複数ある

AWS は「S3 互換」を認定する適合試験を用意していません。代わりに、実装側のプロジェクトが作った非公式の互換性テストが使われています。

代表は Ceph プロジェクトの s3-tests です。boto3 で書かれた多数のテストケースを、実装に対して実行します。README は自らを次のように説明しています。

> This is a set of unofficial Amazon AWS S3 compatibility tests, that can be useful to people implementing software that exposes an S3-like API.
>
> — [ceph/s3-tests README](https://github.com/ceph/s3-tests)、2026-10 時点で取得

このリポジトリは 2026-10-12 にアーカイブされ、テストは Ceph 本体のリポジトリ([`src/test/rgw/s3-tests`](https://github.com/ceph/ceph/tree/main/src/test/rgw/s3-tests))へ移りました。README には「These tests have moved into the https://github.com/ceph/ceph repository and will no longer be maintained here.」とあります。廃止ではなく移転で、移転先では保守が続いています(2026-10 時点)。

Ceph 以外の実装での使われ方を各リポジトリで確認しました(2026-10 時点)。

| 実装 | 互換性テストの扱い |
|---|---|
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | s3-tests を実行するための Dockerfile(`docker/Dockerfile.s3tests`)を持つ |
| [RustFS](https://github.com/rustfs/rustfs) | s3-tests を CI で実行し、通るテストと通らないテストの一覧から互換表(`docs/architecture/s3-compatibility-matrix.md`)を作っている |
| [Garage](https://github.com/deuxfleurs-org/garage) | テスト方針の文書(`doc/book/working-documents/testing-strategy.md`)で、互換性の試験として ceph/s3-tests と [minio/mint](https://github.com/minio/mint) を挙げている |
| [MinIO](https://github.com/minio/minio) | 自前の試験 mint を持つ |

s3-tests は Ceph 以外でも使われる代表的な試験ですが、唯一ではなく、どれも公式ではありません。どの試験に通れば「S3 互換」と名乗れる、という基準もありません。

対応する S3 API の範囲は実装ごとに大きく異なり(各実装が対応 API 一覧を公式に明記しています。例: [SeaweedFS の対応 API 表](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API))、s3-tests のパス状況もそれに応じて実装間で差が出ます(※パス数の横並び比較データは筆者未確認)。**「S3 互換」は二値ではなくグラデーション**です。採用判断では、「S3 互換」という表示ではなく「自分が使う API サブセットで s3-tests を実行した結果」で判断します。

### 3.2 主要なオープンソース実装

| 実装 | 言語 / ライセンス | 特徴 |
|---|---|---|
| [MinIO](https://github.com/minio/minio) | Go / AGPLv3 | 長らく互換実装の代名詞。近年は商用版への注力が進み、2025 年に Community Edition の Web 管理 UI が object browser 中心へ縮小された([object-browser#3509](https://github.com/minio/object-browser/pull/3509))。これを受けて代替を検討する議論も起きている([minio discussion #21320](https://github.com/minio/minio/discussions/21320)) |
| [Ceph RGW](https://github.com/ceph/ceph) | C++ / LGPL | s3-tests 本家。互換性は最も広いが運用は重量級 |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | Go / Apache-2.0 | 小さいファイル大量に強い設計。S3 API はサブセット |
| [Garage](https://git.deuxfleurs.fr/Deuxfleurs/garage) | Rust / AGPLv3 | 自宅・エッジ向けの軽量分散。地理分散前提の設計 |
| [LocalStack](https://github.com/localstack/localstack) | Python | テスト用エミュレータとしての S3 実装 |

商用の互換サービス(Cloudflare R2、Backblaze B2、Wasabi、**さくらのオブジェクトストレージ**等)も、対応する API の一覧をそれぞれの公式ドキュメントで公開しています(§1 の一覧)。各社がどの試験で互換性を確かめているかは公開されておらず、筆者は確認できていません。

## 4. ケーススタディ: 2025 年 1 月の AWS SDK 更新

「片務的な標準」の弱点が表面化した最近の実例です。

- 2024-12: AWS が S3 の[デフォルトのデータ整合性保護](https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-s3-default-data-integrity-protections)を発表 — アップロード時に CRC32/CRC64NVME チェックサム(転送中のデータ破損を検出するための誤り検出符号)を自動付与
- 2025-01: 各言語の AWS SDK がこれを**デフォルト有効**でリリース([aws-sdk-go-v2 の告知](https://github.com/aws/aws-sdk-go-v2/discussions/2960)等)
- 直後: `x-amz-checksum-crc32 ... not implemented` — チェックサム未実装の互換サービス(当時の Cloudflare R2、旧 MinIO、GCS の XML 互換 API など)への**アップロードが相次いで失敗**。[aws-sdk-go-v2 の公式ディスカッション](https://github.com/aws/aws-sdk-go-v2/discussions/2960)にも「サードパーティの S3 互換サービスでは失敗し得る」旨と回避設定が明記されています

回避策として SDK には `when_required` 設定(環境変数 `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` / `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`)が用意されました([AWS 公式: Data Integrity Protections](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html))。互換サービス各社はチェックサム対応を急ぐことになりました。

ここから分かるのは、**「S3 互換」は静的な性質ではなく、AWS の変更に追従し続ける動的なプロセス**だということです。互換ストレージを使うシステムでは、AWS SDK のバージョンアップが「自分は AWS を使っていないのに」破壊的変更になり得ます。

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
2. **使う API のサブセットが動くか** — 一覧・PUT/GET・multipart・presigned まで確認すれば大半のワークロードは足ります。s3-tests を自分で実行して確認します
3. **整合性モデル** — AWS の強整合を前提にしたコード(書いた直後に読む)が互換先でも成立するか
4. **新しめの API(条件付き書き込み、チェックサム等)への依存を避ける** — 互換実装が追いつくまでのタイムラグが常にあります
5. **耐久性と冗長化の方式は、API とは別に確かめる** — 「S3 互換」は API の形が同じというだけで、中の構成は各社で別物です。大手はいずれも大量のディスクに分散させ、消失訂正符号で冗長化していますが、仕組みは独自です(Amazon S3 は大量の HDD の上に作られ([FAST '23 の基調講演](https://www.usenix.org/conference/fast23/presentation/warfield))、保存ノードは独自実装の ShardStore([SOSP 2021 の論文](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3))。Google Cloud Storage は分散ファイルシステム Colossus の上に HDD 主体で SSD を併用:[Google Cloud ブログ](https://cloud.google.com/blog/products/storage-data-transfer/how-colossus-optimizes-data-placement-for-performance)。Azure は独自の符号 LRC:[USENIX ATC 2012](https://www.microsoft.com/en-us/research/publication/erasure-coding-in-windows-azure-storage/))。互換サービスの耐久性の数値は、各社の資料で確認します

## まとめ

- S3 の API 定義は AWS が公開しています(API Reference、2025 年 6 月からは機械可読の AWS API Models も)。ただし中立の標準ではなく、AWS が単独で決め、互換実装がそれを追いかける片務的標準です
- 認証の本体は SigV4。エンドポイント差し替え + path-style + SigV4 が「S3 互換」利用の 3 要素
- 互換性はグラデーション。公式の適合試験はなく、Ceph の s3-tests(2026-10 に Ceph 本体へ移転)や MinIO の mint などの非公式の試験があるだけです。実装ごとの対応 API の差は、各社の公式ドキュメントで確認する必要があります
- 2025 年のチェックサム問題が示す通り、互換性は「維持し続ける作業」。SDK 更新は互換ストレージ利用者にとって破壊的変更になり得ます
- ホスト名の「s3」はプロトコル名。`s3.isk01.sakurastorage.jp` は AWS と無関係のさくらのサービスであり、それでも aws CLI がそのまま使えるのがこの生態系の到達点

## 参考

一次情報(公式ドキュメント・公式リポジトリ):

- [Amazon S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html) / [AWS API Models の公開(2025-06)](https://aws.amazon.com/about-aws/whats-new/2025/06/open-source-aws-api-models) / [SigV4 認証](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html) / [SDK Data Integrity Protections](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html)
- [AWS S3 default data integrity(2024-12 発表)](https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-s3-default-data-integrity-protections) / [aws-sdk-go-v2 の変更告知(公式リポジトリ)](https://github.com/aws/aws-sdk-go-v2/discussions/2960)
- [GCS Interoperability(S3 互換 XML API)](https://cloud.google.com/storage/docs/interoperability) / [Cloudflare R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/) / [Backblaze S3 Compatible API](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api)
- [Ceph の s3-tests(移転先、unofficial 互換性テスト)](https://github.com/ceph/ceph/tree/main/src/test/rgw/s3-tests) / [ceph/s3-tests(2026-10-12 にアーカイブ)](https://github.com/ceph/s3-tests) / [minio/mint](https://github.com/minio/mint)
- [Building and Operating a Pretty Big Storage System(FAST '23)](https://www.usenix.org/conference/fast23/presentation/warfield) / [Using Lightweight Formal Methods to Validate a Key-Value Storage Node in Amazon S3(SOSP 2021)](https://www.amazon.science/publications/using-lightweight-formal-methods-to-validate-a-key-value-storage-node-in-amazon-s3) / [How Colossus optimizes data placement(Google Cloud)](https://cloud.google.com/blog/products/storage-data-transfer/how-colossus-optimizes-data-placement-for-performance) / [Erasure Coding in Windows Azure Storage(USENIX ATC 2012)](https://www.microsoft.com/en-us/research/publication/erasure-coding-in-windows-azure-storage/)
- [MinIO](https://github.com/minio/minio) / [SeaweedFS](https://github.com/seaweedfs/seaweedfs) / [Garage](https://garagehq.deuxfleurs.fr/) / [LocalStack](https://github.com/localstack/localstack)
- [さくらのオブジェクトストレージ マニュアル](https://manual.sakura.ad.jp/cloud/objectstorage/about.html)
