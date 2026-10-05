---
title: "SQLMesh と dbt 1.12 を遅れて届く行と列の追加で比べる"
emoji: "⏱️"
type: "tech"
topics: ["sqlmesh", "dbt", "trino", "iceberg", "dataengineering"]
published: false
---

## はじめに

自宅の Kubernetes クラスタで、NetObserv(eBPF のフロー計測)のデータを Iceberg に貯め、SQLMesh で毎時 bronze → silver → gold に変換しています。運用中に、**silver が bronze より 1 時間あたり 0〜3,714 行少ない**ことに気づきました。原因を切り分けるため、実データで到着順を再現して毎時の処理を模擬し、最新の dbt(dbt-core 1.12.5)とも比べました。

結果は次のとおりです。

- **毎時ちょうどに増分処理を動かすと、lookback を足さない限り、SQLMesh も dbt の microbatch も 1 時間あたり 5,000〜6,000 行を取りこぼしました。** 取りこぼしをなくすのに必要な設定は、SQLMesh では `lookback 1`、dbt では `lookback=2` です。dbt の lookback は実行時点の区間も数に含めるので、1 つずれます
- **列を足したときの挙動は大きく違いました。** SQLMesh は変更を Non-breaking と判定し、処理済みの全区間を自動で作り直しました。dbt は既定の設定のままだと、警告なしに新しい列を無視しました

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.2 |
| dbt | dbt-core 1.12.5 / dbt-trino 1.10.6(PyPI 上の最新、2026-10-05 時点) |
| クエリエンジン | Trino 483 |
| テーブル | Iceberg(カタログは Lakekeeper、保存先は SeaweedFS) |
| 取り込み | pyiceberg を使う自作サービス。受け取ったフローを 60 秒ごとにまとめて追記 |

扱わないものは次のとおりです。

- dbt の次世代エンジン(dbt v2 / Fusion)。Trino に未対応で、対応を求める [dbt-labs/dbt#13131](https://github.com/dbt-labs/dbt/issues/13131) は 2026-10-05 時点で未解決です
- 処理速度の比較
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

## 模擬の方法

実データ(2026-10-04 06:00〜12:00 UTC、1,970,669 行)を、**到着時刻 = 受信時刻 + 60 秒**の順に模擬用の表へ入れました。毎時 0 分(07:00〜13:00)に、その時点で到着済みの行だけが見える状態で、次の方式を「その時刻に実行した」として流しました。13:00 は全行が到着した後の実行です。

| 方式 | 設定 |
|---|---|
| SQLMesh | `INCREMENTAL_BY_TIME_RANGE`、lookback なし(本番と同じ) |
| SQLMesh | 同上に `lookback 1` |
| dbt microbatch | `batch_size='hour'`、lookback は既定値 |
| dbt microbatch | 同上に `lookback=2` |
| dbt の旧来の増分 | `WHERE time_flow_start > (SELECT max(time_flow_start) FROM {{ this }})` で追記 |

SQLMesh は `sqlmesh run --end <時刻>` で、その時刻までの未処理の区間を処理させました。

dbt の microbatch には、実行時刻を外から渡す手段がありません。そこで、公式の説明どおりの範囲を `--event-time-start` と `--event-time-end` で明示しました。[dbt の microbatch のドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)は、通常の実行について次のように説明しています。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

同じドキュメントには、lookback 3 のモデルを 10 月 1 日に実行した例が載っています。

> For example, if you were running the model on October 1, dbt would template separate queries for each day between September 28 and October 1, inclusive — four batches in total.

実行時点の区間(10 月 1 日)も含めて処理するので、毎時 0 分の実行 T では、lookback 1 なら `[T-1h, T+1h)`、lookback 2 なら `[T-2h, T+1h)` を渡しました。検証コードと結果の全体は、末尾の参考に挙げた構成ファイルにあります。

## 結果: 遅れて届く行

時間ごとに、正解(模擬用の表の全行)から各方式の行数を引いた値です。

| 時 | 正解 | SQLMesh lookback なし | SQLMesh lookback 1 | dbt microbatch 既定 | dbt microbatch lookback 2 | dbt 旧来の増分 |
|---|---:|---:|---:|---:|---:|---:|
| 06 | 328,846 | 6,115 | 0 | 6,115 | 0 | 206 |
| 07 | 331,145 | 5,351 | 0 | 5,351 | 0 | 168 |
| 08 | 326,899 | 5,257 | 0 | 5,257 | 0 | 255 |
| 09 | 327,164 | 5,350 | 0 | 5,350 | 0 | 235 |
| 10 | 329,857 | 5,696 | 0 | 5,696 | 0 | 221 |
| 11 | 326,758 | 5,284 | 0 | 5,284 | 0 | 231 |

正解の表には元から同じ内容の行が 34,225 行あり、SQLMesh の lookback 1 の結果も同じ 34,225 行でした。作り直しで重複は増えていません。

### lookback の既定値と数え方

| | 既定値 | 数え方 | 毎時 0 分の実行で、終わったばかりの 1 時間を作り直す設定 |
|---|---|---|---|
| SQLMesh | なし([ソース](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/sqlmesh/core/model/kind.py)で `lookback: Optional[...] = None`) | 処理中の区間より前の区間の数 | `lookback 1` |
| dbt microbatch | 1 | 実行時点の区間を含めた、前の区間の数 | `lookback=2` |

SQLMesh の定義は、[モデルの設定項目の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/overview.md)では次のとおりです。

> It must be a positive integer and specifies how many `interval_unit`s intervals before the current interval the model should include.

dbt の lookback の既定値 1 は、遅れて届く行のための設定です。ただし毎時 0 分に実行すると、その 1 つは実行時点の(まだ空の)区間に使われ、終わったばかりの 1 時間を作り直す区間にはなりません。そのため、今回の模擬では SQLMesh の lookback なしと同じ行数になりました。

### 旧来の増分の欠けが少ない理由

旧来の増分は「表の中の最大時刻より新しい行」を足します。取りこぼすのは、最大時刻より古い時刻を持ったまま遅れて届いた行だけです。毎時の区間で区切る方式は「処理した区間に、後から届いた行」をすべて取りこぼすので、こちらの方が欠けが大きくなりました。

## 結果: 列を足したとき

silver に `COALESCE(tcp_retrans_bytes, 0) AS tcp_retrans_bytes` を 1 列足して流しました。

| | 結果 |
|---|---|
| SQLMesh(`sqlmesh plan`) | `Directly Modified: cmp_sm.flows_lb1 (Non-breaking)` と表示。処理済みの全区間(06:00〜12:59)を作り直し、1,970,669 行すべてに値が入った(11 秒) |
| dbt microbatch、既定(`on_schema_change: ignore`) | `PASS=1 WARN=0`。**新しい列は表に現れなかった** |
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
- Trino で使える最新の dbt は dbt-core 1.12 + dbt-trino で、dbt v2(Fusion)は Trino に未対応です

## 参考

- 検証時の構成ファイル: [sqlmesh(本番のモデルと、模擬の検証コード `verify/late-arrival-vs-dbt`)](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/sqlmesh)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public)。環境固有値はダミーに置換済み)

- [dbt: About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)
- [dbt: Configure incremental models](https://docs.getdbt.com/docs/build/incremental-models)
- [dbt-labs/dbt#13131: Release Starburst Trino support](https://github.com/dbt-labs/dbt/issues/13131)
- [SQLMesh v0.236.2: Model kinds](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/model_kinds.md)
- [SQLMesh v0.236.2: Models overview(lookback)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/overview.md)
- [SQLMesh v0.236.2: kind.py](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/sqlmesh/core/model/kind.py)
- [dbt-core(PyPI)](https://pypi.org/project/dbt-core/)
- [dbt-trino(PyPI)](https://pypi.org/project/dbt-trino/)
