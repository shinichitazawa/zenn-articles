---
title: "遅延到着データと列の追加に対する SQLMesh と dbt 2.0 の挙動"
emoji: "⏱️"
type: "tech"
topics: ["sqlmesh", "dbt", "duckdb", "trino", "dataengineering"]
published: false
---

## はじめに

自宅の Kubernetes クラスタで、NetObserv(eBPF でネットワークのフローを計測するツール)のデータを Iceberg の表に貯めています。データは 3 段の表で管理しています。

| 表 | 中身 | 作り方 |
|---|---|---|
| bronze | 取り込んだフローの記録そのまま。1 行 = 1 フロー | 取り込みサービスが 60 秒ごとにまとめて追記する |
| silver | bronze を整えた表。1 行 = 1 フロー | SQLMesh が毎時、bronze から作る |
| gold | silver を時間・namespace などで集計した表 | SQLMesh が毎時、silver から作る |

silver は増分処理で作っています。増分処理とは、毎回すべてを作り直さず、新しく増えた分だけを処理する方式です。silver では、フローの開始時刻(`time_flow_start`)で 1 時間ごとに区切った「区間」を単位にして、毎時、終わったばかりの 1 時間の区間を bronze から作ります。

運用中に、**silver の行数が bronze より、1 時間の区間あたり 0〜3,714 行少ない**ことに気づきました。原因を切り分けるため、実データの到着の順番を再現して毎時の処理を模擬し、dbt の最新版とも比べました。

結果は次のとおりです。

- **毎時 0 分に増分処理を動かすと、SQLMesh も dbt も、区間を作り直す設定(lookback)を足さない限り、1 時間あたり 5,000〜6,000 行を取りこぼしました。** 取りこぼしが無くなる設定は、SQLMesh では `lookback 1`、dbt では `lookback=2` です。値が 1 ずれるのは、dbt の lookback が実行時点の区間も数に含めるためです
- **モデルに列を足したとき、変更を本番に入れる手順と既定の動作が違いました。** SQLMesh は `plan`(変更を本番に入れる手順)で変更の種類を表示し、適用すると処理済みの全区間を作り直しました。dbt は実行と同時に変更が入り、既定の設定では警告なしに新しい列を無視しました
- **上流の表に列が増えただけ(モデルは `SELECT *`)のときは、どちらも既定では新しい列を黙って無視しました**

比べた版は、2026-10-11 時点のそれぞれの最新の安定版です。

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.3 |
| dbt | 2.0.5([GitHub のリリース](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)。Rust で書き直された版) |
| エンジン | DuckDB 1.5.6 |
| 入力 | 本番の bronze から書き出したフローの記録 1,970,669 行(フローの開始時刻が 2026-10-04 06:00:00〜11:59:59 UTC のもの) |

本番のエンジンは Trino ですが、dbt 2.0 の[対応表](https://docs.getdbt.com/docs/fusion/supported-features)に Trino は無いので(2026-10-11 時点)、両方が動く DuckDB に移して比べました。同じ試験を本番と同じ Trino 483 の上でも、1 つ前の組み合わせ(dbt-core 1.12.5 / dbt-trino 1.10.6、SQLMesh 0.236.2)で行い、取りこぼした行数はすべての区間で一致しました。

扱わないものは次のとおりです。

- 処理速度の比較
- dbt platform の機能。dbt 2.0 の `dbt State` は、[dbt platform のアカウントで動き、最終更新時刻と SQL のハッシュを dbt Labs のサーバーに送る](https://docs.getdbt.com/docs/deploy/dbt-state-about)機能なので、自前で動かす構成の比較から外しました
- dbt Cloud や SQLMesh の UI などの周辺機能

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 本番で起きていたこと

silver は、SQLMesh の `INCREMENTAL_BY_TIME_RANGE`(時刻の列で区間を区切る増分処理)で、`cron '@hourly'`(1 時間の区間を単位にする設定)です。2026-10-04〜05 の bronze と silver の行数を、フローの開始時刻の 1 時間ごとに比べると、silver が少ない区間が多くありました。

| フローの開始時刻の区間(UTC) | bronze の行数 − silver の行数 |
|---|---:|
| 10-04 12 時台〜15 時台 | 1,166 / 2,065 / 2,309 / 3,714 |
| 10-04 16 時台・17 時台 | 0 / 0 |
| 10-05 03 時台〜07 時台 | 533 / 1,203 / 1,102 / 1,665 / 2,910 |
| 10-05 09 時台 | 2,255 |

取り込みサービスがフローの記録を受け取った時刻(`time_received`)も見ました。フローの開始時刻の区間が終わった後に受け取った行は、1 時間あたり 250 行前後で、区間の終わりからの遅れは最大でも 4 秒でした。受け取りの遅れだけでは、数千行の差を説明できません。

差の大半は、**受け取り済みでも、まだ bronze に書き込まれていない行**と考えました。取り込みサービスは受け取った行を 60 秒ごとにまとめて書き込むので、ある区間の最後の 1 分前後の行は、区間が終わってから最大 60 秒後まで bronze に現れません。毎時 0 分に SQLMesh が終わったばかりの区間を読むと、その行がまだ入っていない可能性があります。

このように、行の時刻が属する区間を処理した後で表に入るデータを、遅延到着データ(late-arriving data)と呼びます。dbt の [microbatch の説明](https://docs.getdbt.com/docs/build/incremental-microbatch)では、`lookback` の目的を次のように書いています。

> Process X batches prior to the latest bookmark to capture late-arriving records.

lookback は、終わった区間をあとの実行でもう一度作り直す設定です。作り直す時点で遅延到着データが到着していれば、取りこぼしが埋まります。

## 模擬の方法

### 到着の再現

入力の各行が bronze に現れる時刻(到着時刻)を、**受け取った時刻 + 60 秒**としました。取り込みサービスの書き込み間隔が 60 秒なので、どの行も遅くとも受け取りの 60 秒後には現れます。今回はその最も遅い場合を全行に当てはめました。

模擬用の表に、到着時刻の順に行を入れ、毎時 0 分(07:00〜13:00)の時点で到着済みの行だけが見える状態で各方式を実行しました。13:00 は、11 時台の最後の行(11:59:59 に受け取った行は 12:00:59 に到着)まで全行が到着した後の実行です。

### 比べた方式

| 方式 | 何をするか |
|---|---|
| SQLMesh、lookback なし(本番と同じ) | 実行のたびに、まだ処理していない区間を処理する |
| SQLMesh、`lookback 1` | 上に加えて、その 1 つ前の区間を作り直す |
| dbt microbatch、lookback 既定(1) | 実行時刻の区間と、その 1 つ前の区間を処理する |
| dbt microbatch、`lookback=2` | 実行時刻の区間と、その 2 つ前までの区間を処理する |
| dbt の従来の増分(watermark 方式) | 出力の表にある最大の時刻より新しい行だけを追記する(`WHERE time_flow_start > (SELECT max(time_flow_start) FROM {{ this }})`) |

たとえば 09:00 の実行では、各方式が処理する区間は次のようになります。

| 方式 | 09:00 の実行で処理する区間 | 08 時台の区間を作るのは |
|---|---|---|
| SQLMesh、lookback なし | 08 時台 | 09:00 の 1 回だけ |
| SQLMesh、`lookback 1` | 07 時台(作り直し)、08 時台 | 09:00 と 10:00 の 2 回 |
| dbt、lookback 1 | 08 時台、09 時台(まだ空) | 09:00 の 1 回だけ |
| dbt、`lookback=2` | 07 時台(作り直し)、08 時台、09 時台(まだ空) | 09:00 と 10:00 の 2 回 |

08 時台の区間を 09:00 の 1 回しか作らない方式は、08 時台の最後の 1 分前後の行(09:00 にはまだ到着していない)を取りこぼします。10:00 にもう一度作る方式は、その時点で全行が到着しているので取りこぼしません。

### 実行のさせ方

SQLMesh は `sqlmesh run --end <時刻>` で、その時刻までに終わった区間を処理させました。

dbt の microbatch は、[公式ドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)のとおり、実行した時刻と `lookback` から処理する区間を決めます。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

dbt 2.0.5 には実行時刻を外から与えるオプションが無いので、[libfaketime](https://github.com/wolfcw/libfaketime) で dbt から見える時計だけを各時刻の 20 秒後(09:00:20 など)にずらしました。処理する区間は dbt 自身が決めています。

## 結果: 遅延到着データ

フローの開始時刻の区間ごとに、各方式が**取りこぼした行数**(入力の行数 − 出力の表の行数)です。0 なら取りこぼしなしです。

| 区間(UTC) | 入力の行数 | SQLMesh lookback なし | SQLMesh `lookback 1` | dbt lookback 1(既定) | dbt `lookback=2` | dbt watermark 方式 |
|---|---:|---:|---:|---:|---:|---:|
| 06 時台 | 328,846 | 6,115 | 0 | 6,115 | 0 | 206 |
| 07 時台 | 331,145 | 5,351 | 0 | 5,351 | 0 | 168 |
| 08 時台 | 326,899 | 5,257 | 0 | 5,257 | 0 | 255 |
| 09 時台 | 327,164 | 5,350 | 0 | 5,350 | 0 | 235 |
| 10 時台 | 329,857 | 5,696 | 0 | 5,696 | 0 | 221 |
| 11 時台 | 326,758 | 5,284 | 0 | 5,284 | 0 | 231 |

取りこぼした約 5,000〜6,000 行は、各区間の最後の 1 分前後の行(1 時間の約 1.6〜1.9%)にあたります。

本番の差(0〜3,714 行)より模擬の方が多いのは、模擬では全行が受け取りの 60 秒後に現れるとした(最も遅い場合)ためと考えられます。本番では書き込みの時刻が区間の終わりとずれるので、取りこぼす行は 60 秒分より少なくなります。本番で差が 0 の区間があるのも、この時刻のずれで説明できますが、区間ごとの書き込み時刻までは確かめていません。

### lookback の既定値と数え方

| | 既定値 | lookback が数える区間 | 毎時 0 分の実行で、終わったばかりの区間を次の実行でも作り直す設定 |
|---|---|---|---|
| SQLMesh | なし([ソース](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/sqlmesh/core/model/kind.py)で `lookback: Optional[...] = None`) | 処理する区間より前の区間 | `lookback 1` |
| dbt microbatch | 1 | 実行時刻の区間より前の区間 | `lookback=2` |

SQLMesh の定義は、[モデルの設定項目の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md)では次のとおりです。

> It must be a positive integer and specifies how many `interval_unit`s intervals before the current interval the model should include.

dbt の既定値 1 も遅延到着データのための設定です。ただし毎時 0 分に実行すると、実行時刻の区間はまだ始まったばかりで、lookback 1 で処理する 1 つ前の区間が「終わったばかりの区間」になります。その区間を作るのはこの 1 回だけなので、次の実行で作り直されることはありません。結果として、SQLMesh の lookback なしと同じ行数を取りこぼしました。

### watermark 方式の取りこぼしが少ない理由

watermark 方式は、区間で区切らず、「出力の表にある最大の時刻より新しい行」を毎回足します。実行の時点で到着していなかった行も、時刻が最大値より新しければ次の実行で足されます。

取りこぼすのは、時刻が最大値より古い遅延到着データだけです。フローの記録は受け取った順に書き込まれ、開始時刻の順にはなっていません。入力を受け取った順に並べると、97.5% の行は、先に受け取ったどれかの行より開始時刻が古い状態でした(開始から受け取りまでの時間は最大 5 秒、中央値 3 秒)。同じ書き込みの中に開始時刻の古い行と新しい行が混ざります。新しい行が先に書き込まれて最大値が進むと、あとから到着した古い行は条件から外れます。これが 1 時間あたり 168〜255 行でした。区間で区切る方式は「処理済みの区間に後から到着した行」をすべて取りこぼすので、こちらの方が取りこぼしが 1 桁多くなりました。

## 結果: 列を足したとき

列の追加は、2 つの場合に分けて試しました。

1. **モデルの SQL を書き換えて列を足した場合**: silver の SELECT に `COALESCE(tcp_retrans_bytes, 0) AS tcp_retrans_bytes` を足しました
2. **上流の表に列が増えた場合**: モデルは `SELECT *` のまま書き換えず、入力の表に `packets` 列を足しました

### モデルの SQL を書き換えた場合

2 つのツールは、変更を本番に入れる手順が違います。SQLMesh は、モデルのファイルを書き換えただけでは本番に入りません。`sqlmesh plan` が変更の種類と作り直す区間を表示し、確認してから適用します。dbt には別の手順が無く、`dbt run` のときにそのまま新しい SQL が使われます。

| 手順 | 結果 |
|---|---|
| SQLMesh、`sqlmesh run` だけ | **変更は反映されない**。本番に入れた前の版のまま実行され、列は増えない |
| SQLMesh、`sqlmesh plan`(今回は `--auto-apply` で確認を省いて適用) | `Directly Modified: sm.late_lb1 (Non-breaking)` と表示。適用すると処理済みの全区間(06:00〜12:59)を作り直し、1,970,669 行すべてに値が入った |
| SQLMesh、`sqlmesh plan --forward-only` | 列を追加し、過去の区間は作り直さない。過去の行の新しい列は NULL |
| dbt、`on_schema_change` 既定(`ignore`)で `dbt run` | 実行は成功。**新しい列は表に現れず、警告もない** |
| dbt、`on_schema_change: append_new_columns` で `dbt run` | 列が追加される。値が入るのは処理した区間だけで、それより前の行は NULL |

Non-breaking は、SQLMesh が変更を分類する種類の 1 つです。[plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)によると、Non-breaking に分類されたモデルは作り直され、下流のモデルは作り直されません。列の追加が典型例として挙がっています。

> A directly-modified model that is classified as non-breaking will be backfilled, but its downstream dependencies will not.

作り直さずに列だけ足したい場合は、forward-only の変更として適用します(`plan --forward-only`)。同じページによると、forward-only の変更は本番で既存の物理表を使い続け、作り直しは行われません。

dbt の挙動は、[増分モデルのドキュメント](https://docs.getdbt.com/docs/build/incremental-models)の説明どおりです。既定の `ignore` については次のとおりです。

> If you add a column to your incremental model, and execute a `dbt run`, this column will _not_ appear in your target table.

`append_new_columns` でも過去の行が埋まらないことが、同じドキュメントに書かれています。

> **Note**: None of the `on_schema_change` behaviors backfill values in old records for newly added columns.

dbt で過去の行まで埋めるには、`--full-refresh` か、範囲を指定した実行を人が判断して流します。

### 上流の表に列が増えた場合(`SELECT *`)

| | 既定の普通の実行 | 列の追加を検出する方法 |
|---|---|---|
| SQLMesh | **新しい列を無視**。警告なし。`sqlmesh plan` も「No changes」 | 上流の表の定義を YAML に書き出しておき(`sqlmesh create_external_models`)、列が増えた後に作り直して `plan` を流すと、モデルが Breaking の変更として表示された |
| dbt | **新しい列を無視**。警告なし | `on_schema_change: fail` にすると、実行が失敗して止まる。`append_new_columns` / `sync_all_columns` では列が増え、値は処理した区間だけに入る |

上流の変化に対しては、どちらも既定では何もしません。SQLMesh の YAML の定義を使うと `SELECT *` がその定義の列に展開されるので、YAML を作り直したときに展開結果の変化として検出されます。

## 本番での扱い

本番の silver には、まだ `lookback 1` を入れていません。入れると毎時、直前の 2 区間を作ることになり、処理する行数がおよそ 2 倍になります。処理時間と Trino の負荷を測ってから入れる予定です。

## まとめ

- 毎時 0 分に増分処理を動かすと、各区間の最後の 1 分前後に書き込まれる行を取りこぼしました。SQLMesh の lookback なしでも、dbt の lookback 既定でも、1 時間あたり 5,000〜6,000 行です
- 取りこぼしが無くなる設定は、SQLMesh では `lookback 1`、dbt では `lookback=2` でした。dbt の lookback は実行時刻の区間から数えるので、1 つずれます
- watermark 方式(最大の時刻より新しい行を足す)は、時刻の順番が入れ替わった行だけを取りこぼし、区間で区切る方式より取りこぼしが 1 桁少ない結果でした
- モデルを書き換えて列を足すと、SQLMesh は `plan` で Non-breaking と表示し、適用すると全区間を作り直しました(`--forward-only` で作り直さない選択もある)。dbt は実行と同時に変更が入り、既定では列を警告なしに無視しました
- 上流の表に列が増えただけの場合は、どちらも既定では新しい列を無視しました

## 参考

- 検証時の構成ファイル: [sqlmesh(本番のモデルと、模擬の検証コード `verify/late-arrival-vs-dbt`)](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/sqlmesh)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public)。環境固有値はダミーに置換済み)。DuckDB で最新の版を比べたコードは未公開です

- [dbt: About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)
- [dbt: Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)
- [dbt v2: Supported features(対応するデータプラットフォーム)](https://docs.getdbt.com/docs/fusion/supported-features)
- [dbt: About dbt State](https://docs.getdbt.com/docs/deploy/dbt-state-about)
- [dbt-labs/dbt-core v2.0.5](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)
- [SQLMesh v0.236.3: Model kinds](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/model_kinds.md)
- [SQLMesh v0.236.3: Models overview(lookback)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md)
- [SQLMesh v0.236.3: Plans(変更の分類と forward-only)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)
- [SQLMesh v0.236.3: kind.py](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/sqlmesh/core/model/kind.py)
- [libfaketime](https://github.com/wolfcw/libfaketime)
