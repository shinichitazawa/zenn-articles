---
title: "遅延到着データと列の追加に対する SQLMesh と dbt 2.0 の挙動"
emoji: "⏱️"
type: "tech"
topics: ["sqlmesh", "dbt", "duckdb", "trino", "dataengineering"]
published: false
---

## はじめに

自宅の Kubernetes クラスタで、NetObserv(eBPF のフロー計測)のデータを Iceberg に貯め、SQLMesh で毎時 bronze → silver → gold に変換しています。運用中に、**silver が bronze より 1 時間あたり 0〜3,714 行少ない**ことに気づきました。原因を切り分けるため、実データで到着順を再現して毎時の処理を模擬し、最新の dbt とも比べました。

結果は次のとおりです。

- **毎時ちょうどに増分処理を動かすと、lookback を足さない限り、SQLMesh も dbt の microbatch も 1 時間あたり 5,000〜6,000 行を取りこぼしました。** 取りこぼしをなくすのに必要な設定は、SQLMesh では `lookback 1`、dbt では `lookback=2` です。dbt の lookback は実行時点の区間も数に含めるので、1 つずれます
- **列を足したときの挙動は大きく違いました。** SQLMesh は変更を Non-breaking と判定し、処理済みの全区間を自動で作り直しました。dbt は既定の設定のままだと、警告なしに新しい列を無視しました

比べた版は、2026-10-11 時点のそれぞれの最新の安定版です。

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.3 |
| dbt | 2.0.5([GitHub のリリース](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)。Rust で書き直された版) |
| エンジン | DuckDB 1.5.6 |
| 入力 | 本番の bronze から書き出した 1,970,669 行(2026-10-04 06:00〜11:59 UTC) |

本番のエンジンは Trino ですが、dbt 2.0 の[対応表](https://docs.getdbt.com/docs/fusion/supported-features)に Trino は無いので(2026-10-11 時点)、両方が動く DuckDB に移して比べました。同じ試験を、本番と同じ Trino 483 の上で dbt-core 1.12.5 / dbt-trino 1.10.6(PyPI の安定版の最新)と SQLMesh 0.236.2 でも行い、取りこぼしの行数は表のすべての値で一致しました。

扱わないものは次のとおりです。

- 処理速度の比較
- dbt platform の機能。dbt 2.0 の `dbt State` は、[dbt platform のアカウントで動き、最終更新時刻と SQL のハッシュを dbt Labs のサーバーに送る](https://docs.getdbt.com/docs/deploy/dbt-state-about)機能なので、自前で動かす構成の比較から外しました
- dbt Cloud や SQLMesh の UI などの周辺機能

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 本番で起きていたこと

silver のモデルは、SQLMesh の `INCREMENTAL_BY_TIME_RANGE` で `cron '@hourly'` です。毎時、直前の 1 時間分を bronze から作ります。2026-10-04〜05 の 1 日分を時間ごとに比べると、silver の行数が bronze より少ない時間帯が多くありました。

| 時間帯(UTC) | bronze − silver |
|---|---:|
| 10-04 12 時〜15 時 | 1,166 / 2,065 / 2,309 / 3,714 |
| 10-04 16 時・17 時 | 0 / 0 |
| 10-05 03 時〜07 時 | 533 / 1,203 / 1,102 / 1,665 / 2,910 |
| 10-05 09 時 | 2,255 |

受信時刻(`time_received`)で見た遅れは小さく、1 時間に 250 行前後、最大でも 4 秒でした。欠けの大半は、**受信済みでもまだテーブルに書かれていない行**と考えました。取り込みサービスは 60 秒ごとにまとめて追記するので、毎時 0 分に SQLMesh が直前の 1 時間を読む時点では、最後の 1 分前後の行がまだ入っていない可能性があります。

このように、イベントの時刻が属する区間を処理した後でテーブルに入るデータを、本記事では遅延到着データと呼びます。dbt の [microbatch の設定項目の説明](https://docs.getdbt.com/docs/build/incremental-microbatch)では、`lookback` を次のように説明しています。

> Process X batches prior to the latest bookmark to capture late-arriving records.

## 模擬の方法

入力の行を、**到着時刻 = 受信時刻 + 60 秒**の順に模擬用の表へ入れました。毎時 0 分(07:00〜13:00)に、その時点で到着済みの行だけが見える状態で、次の方式を実行しました。13:00 は全行が到着した後の実行です。

| 方式 | 設定 |
|---|---|
| SQLMesh | `INCREMENTAL_BY_TIME_RANGE`、lookback なし(本番と同じ) |
| SQLMesh | 同上に `lookback 1` |
| dbt microbatch | `batch_size='hour'`、lookback は既定値 |
| dbt microbatch | 同上に `lookback=2` |
| dbt の従来の増分 | `WHERE time_flow_start > (SELECT max(time_flow_start) FROM {{ this }})` で追記 |

SQLMesh は `sqlmesh run --end <時刻>` で、その時刻までの未処理の区間を処理させました。

dbt の microbatch は、[公式ドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)のとおり、実行した時刻と `lookback` から処理する区間を決めます。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

dbt 2.0.5 には実行時刻を外から与えるオプションが無いので、[libfaketime](https://github.com/wolfcw/libfaketime) で dbt から見える時計だけを各時刻の 20 秒後にずらしました。処理する区間は dbt 自身が決めています。検証コードと結果の全体は、末尾の参考に挙げた構成ファイルにあります。

## 結果: 遅延到着データ

時間ごとに、正解(入力の全行)から各方式の行数を引いた値です。

| 時 | 正解 | SQLMesh lookback なし | SQLMesh lookback 1 | dbt microbatch 既定 | dbt microbatch lookback 2 | dbt 旧来の増分 |
|---|---:|---:|---:|---:|---:|---:|
| 06 | 328,846 | 6,115 | 0 | 6,115 | 0 | 206 |
| 07 | 331,145 | 5,351 | 0 | 5,351 | 0 | 168 |
| 08 | 326,899 | 5,257 | 0 | 5,257 | 0 | 255 |
| 09 | 327,164 | 5,350 | 0 | 5,350 | 0 | 235 |
| 10 | 329,857 | 5,696 | 0 | 5,696 | 0 | 221 |
| 11 | 326,758 | 5,284 | 0 | 5,284 | 0 | 231 |

### lookback の既定値と数え方

| | 既定値 | 数え方 | 毎時 0 分の実行で、終わったばかりの 1 時間を作り直す設定 |
|---|---|---|---|
| SQLMesh | なし([ソース](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/sqlmesh/core/model/kind.py)で `lookback: Optional[...] = None`) | 処理中の区間より前の区間の数 | `lookback 1` |
| dbt microbatch | 1 | 実行時点の区間を含めた、前の区間の数 | `lookback=2` |

SQLMesh の定義は、[モデルの設定項目の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md)では次のとおりです。

> It must be a positive integer and specifies how many `interval_unit`s intervals before the current interval the model should include.

dbt の lookback の既定値 1 は、遅延到着データのための設定です。ただし毎時 0 分に実行すると、その 1 つは実行時点の(まだ空の)区間に使われ、終わったばかりの 1 時間を作り直す区間にはなりません。そのため、今回の模擬では SQLMesh の lookback なしと同じ行数になりました。

### 旧来の増分の欠けが少ない理由

旧来の増分は「表の中の最大時刻より新しい行」を足します。取りこぼすのは、最大時刻より古い時刻を持つ遅延到着データだけです。毎時の区間で区切る方式は「処理済みの区間に後から到着したデータ」をすべて取りこぼすので、こちらの方が欠けが大きくなりました。

## 結果: 列を足したとき

silver に `COALESCE(tcp_retrans_bytes, 0) AS tcp_retrans_bytes` を 1 列足し、13:05 の実行として流しました。

| | 結果 |
|---|---|
| SQLMesh(`sqlmesh plan`) | `Directly Modified: sm.late_lb1 (Non-breaking)` と表示。処理済みの全区間(06:00〜12:59)を作り直し、1,970,669 行すべてに値が入った(3.2 秒) |
| dbt microbatch、既定(`on_schema_change: ignore`) | 実行は成功(`1 success`)。**新しい列は表に現れなかった** |
| dbt microbatch、`on_schema_change: append_new_columns` | 列は追加されたが、**値が入ったのは流した区間(11 時台)だけ**で、06〜10 時は NULL のまま |

dbt の挙動は、[増分モデルのドキュメント](https://docs.getdbt.com/docs/build/incremental-models)の説明どおりです。既定の `ignore` については次のとおりです。

> If you add a column to your incremental model, and execute a `dbt run`, this column will _not_ appear in your target table.

`append_new_columns` で過去の行が NULL のままになることも、同じドキュメントに書かれています。

> **Note**: None of the `on_schema_change` behaviors backfill values in old records for newly added columns.

dbt で過去の行まで埋めるには、`--full-refresh` か、範囲を指定した実行を人が判断して流す必要があります。SQLMesh は、モデルの変更を検出した時点で、作り直す範囲を自分で決めました。

## 本番での扱い

本番の silver には、まだ `lookback 1` を入れていません。入れると、毎時の処理で直前の 2 時間分を書き直すことになるので、処理時間と Trino の負荷を測ってから入れる予定です。

## まとめ

- 毎時ちょうどに増分処理を動かすと、書き込みの遅れの分を毎時取りこぼしました。SQLMesh の lookback なしでも、dbt の microbatch の既定でも、1 時間あたり 5,000〜6,000 行です
- 取りこぼしをなくすのに必要な設定は、SQLMesh では `lookback 1`、dbt では `lookback=2` でした。dbt の lookback は実行時点の区間も数えるので、1 つずれます
- dbt の旧来の増分(最大時刻より新しい行を足す)は、区間方式より欠けが 1 桁少ない結果でした
- 列を足すと、SQLMesh は Non-breaking と判定して全区間を自動で作り直しました。dbt は既定で警告なしに列を無視し、`append_new_columns` でも過去の行は NULL のままでした
- 取りこぼしの行数は、dbt 2.0.5 + DuckDB と dbt-core 1.12.5 + Trino の 2 通りで同じでした

## 参考

- 検証時の構成ファイル: [sqlmesh(本番のモデルと、模擬の検証コード `verify/late-arrival-vs-dbt`)](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/sqlmesh)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public)。環境固有値はダミーに置換済み)。DuckDB で最新の版を比べたコードは未公開です

- [dbt: About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)
- [dbt: Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)
- [dbt v2: Supported features(対応するデータプラットフォーム)](https://docs.getdbt.com/docs/fusion/supported-features)
- [dbt: About dbt State](https://docs.getdbt.com/docs/deploy/dbt-state-about)
- [dbt-labs/dbt-core v2.0.5](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)
- [dbt-core(PyPI)](https://pypi.org/project/dbt-core/)
- [SQLMesh v0.236.3: Model kinds](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/model_kinds.md)
- [SQLMesh v0.236.3: Models overview(lookback)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md)
- [SQLMesh v0.236.3: kind.py](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/sqlmesh/core/model/kind.py)
- [libfaketime](https://github.com/wolfcw/libfaketime)
