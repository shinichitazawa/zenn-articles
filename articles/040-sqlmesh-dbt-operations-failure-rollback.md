---
title: "失敗・データの訂正・ロールバックに対する SQLMesh と dbt 2.0 の挙動"
emoji: "🔁"
type: "tech"
topics: ["sqlmesh", "dbt", "duckdb", "iceberg", "dataengineering"]
published: false
---

## はじめに

[前回の記事](https://zenn.dev/shinichitazawa/articles/039-sqlmesh-dbt-late-arrival-schema-change)では、遅延到着データと列の追加に対する SQLMesh と dbt の挙動を比べました。本記事はその続きで、データパイプラインの運用中に起きる次の場面を、同じ入力で両方に再現しました。

- 実行が飛んだとき、一部の区間だけ失敗したとき
- 上流の過去データが後から訂正されたとき
- モデルの式・列・型を変えたとき、その変更を戻すとき
- 履歴を持つ表(SCD Type 2)、外部データの到着待ち、変更の影響範囲、状態 DB の障害、ツールの更新、大量の再処理

結果は次のとおりです。

- **差が大きいのは「後から直す」場面でした。** SQLMesh は失敗した区間や飛んだ区間を状態 DB に記録していて、次の普通の実行で取り返しました。dbt は、人が範囲を指定して流さない限り、空いた区間や新旧の式の混在が「成功」のまま残りました
- **dbt 2.0.5 では、microbatch のバッチが 1 つ失敗すると後ろのバッチも実行されず、`dbt retry` でも取り返せませんでした。** 前回の実行結果のファイルに、失敗したモデルが記録されていなかったためです
- **ロールバックは、SQLMesh が計算なし(作り直した区間 0)、dbt は全期間の計算し直しでした**
- **SQLMesh には、状態 DB が止まると変換がすべて止まる、前の版の表のぶん容量を使う、という代償がありました**

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.3 |
| dbt | 2.0.5([GitHub のリリース](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)) |
| エンジン | DuckDB 1.5.6 |
| 入力 | 本番の bronze から書き出した 1,970,669 行(2026-10-04 06:00〜11:59 UTC) |

版は 2026-10-11 時点のそれぞれの最新の安定版です。本番のエンジンは Trino ですが、dbt 2.0 の[対応表](https://docs.getdbt.com/docs/fusion/supported-features)に Trino が無いので、両方が動く DuckDB で比べました。本番と同じ Trino + Iceberg の上で、1 つ前の組み合わせ(SQLMesh 0.236.2 / dbt-core 1.12.5)でも同じ試験をしており、エンジンに固有の結果は後の節に分けて書きます。

扱わないものは次のとおりです。

- 処理速度の比較。DuckDB では 197 万行の処理がどれも数秒で終わるので、所要時間ではなく「どの区間を計算し直したか」で比べます
- dbt platform の機能。dbt 2.0 の `dbt State` は、[dbt platform のアカウントで動き、最終更新時刻と SQL のハッシュを dbt Labs のサーバーに送る](https://docs.getdbt.com/docs/deploy/dbt-state-about)機能なので、自前で動かす構成の比較から外しました
- オーケストレーター(Argo Workflows など)の比較

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 検証の方法

どちらのツールでも、silver(フロー 1 行ずつ)→ gold(時間・namespace ごとの集計)の 2 段のモデルを、1 時間の区間で作りました。

| | SQLMesh | dbt |
|---|---|---|
| silver | `INCREMENTAL_BY_TIME_RANGE`、`batch_size 1` | microbatch、`batch_size='hour'`、`lookback=1` |
| gold | `INCREMENTAL_BY_TIME_RANGE` | microbatch(silver に `event_time` を設定) |
| 実行時刻の与え方 | `sqlmesh run --end <時刻>` | [libfaketime](https://github.com/wolfcw/libfaketime) で dbt から見える時計をずらす |

dbt の microbatch は、[公式ドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)のとおり、実行した時刻と `lookback` から処理する区間を決めます。dbt 2.0.5 には実行時刻を外から与えるオプションが無いので、時計だけをずらし、区間は dbt 自身に決めさせました。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

失敗は、DuckDB の [`error()`](https://duckdb.org/docs/current/sql/functions/utility.html) 関数で 9 時台の行だけに起こしました。制御用の表に時間を書くと、その時間の行で `error()` が呼ばれるようにしてあります。

## 実行が飛んだとき

07:00 に初回を実行したあと、08〜10 時の実行を飛ばし、11 時に実行しました。

| 時 | 入力 | SQLMesh | dbt |
|---|---:|---:|---:|
| 06 | 328,846 | 328,846 | 328,846 |
| 07 | 331,145 | 331,145 | 331,145 |
| 08 | 326,899 | 326,899 | 0 |
| 09 | 327,164 | 327,164 | 0 |
| 10 | 329,857 | 329,857 | 329,857 |

SQLMesh は、11:00 の実行で 07〜10 時の 4 区間を処理しました。dbt(11:05 の実行)が処理したのは 10 時(lookback の分)と 11 時(実行時刻の区間)だけで、08・09 時は空いたまま実行が成功しました。空いた区間は、範囲を指定して流せば埋まります。

```bash
dbt run --select ops_silver+ \
  --event-time-start "2026-10-04 08:00:00" --event-time-end "2026-10-04 10:00:00"
```

## 一部の区間だけ失敗したとき

9 時台だけを失敗させて 6 時間分を処理し、失敗の原因を取り除いてからもう一度実行しました。

| | SQLMesh 0.236.3 | dbt 2.0.5 |
|---|---|---|
| 1 回目の実行 | 9 時だけ失敗。成功した 5 区間は状態 DB に記録され、gold は上流が揃わないので保留 | silver は 06〜08 時を処理したところで失敗し、**10・11 時のバッチは実行されなかった**。gold はスキップ |
| 前回の実行結果(`run_results.json`) | — | **gold の「スキップ」1 件だけ。失敗した silver の記録が無い** |
| 2 回目(原因を除いた後) | 普通の `sqlmesh run` で 9 時だけを処理し、そのあと gold を作った | [`dbt retry`](https://docs.getdbt.com/reference/commands/retry) は gold だけを作り直して「成功」。**silver の 9〜11 時は 0 行のまま** |
| retry せず次の定時実行(12:05) | — | 11・12 時だけを処理して成功。**9・10 時は 0 行のまま** |

dbt 2.0.5 の結果は、1 つ前の dbt-core 1.12.5(Trino)と違いました。1.12.5 では、失敗したバッチを飛ばして 10・11 時を処理し(`Completed with 1 partial success`)、`dbt retry` で 9 時だけを処理し直せました。2.0.5 での挙動は 2 回試して同じでした。dbt-core の issue を検索しましたが、該当するものは見つかりませんでした(2026-10-11 時点)。

`dbt retry` は前回の実行結果のファイルを読む仕組みなので、実行のたびにコンテナを作り直す環境では、ファイルを保存しておかない限り使えません。SQLMesh は状態 DB に記録しているので、次の普通の実行で取り返せました。

## 上流の過去データが訂正されたとき

入力の 8 時台の `bytes` を 0 に書き換えて、上流で過去のデータが訂正された状況を作りました。

| 操作 | SQLMesh | dbt |
|---|---|---|
| 普通の実行 | 拾わない(silver・gold とも元の値) | 拾わない |
| 範囲を指定した再処理 | `plan --restate-model sm.ops_silver --start … --end …` で silver が直り、**gold も自動で作り直された** | `--event-time-start/--end` で直る。silver だけを選ぶと gold は古い値のまま。gold まで直すには `+`(`--select ops_silver+`)が要る |
| 同じ範囲を 2 回流す | 件数・値とも変わらない | 件数・値とも変わらない |

SQLMesh の作り直しが下流に伝わるのは、[plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)のとおりでした。

> Applying a restatement plan will trigger a cascading backfill for all selected models, as well as all models downstream from them.

dbt の microbatch は区間ごとに「消してから入れ直す」ので、同じ区間を何度流しても重複しません。上流の訂正を検知する機能は、どちらにもありませんでした。

## モデルを変えたとき

### 式の変更

silver の `bytes` を `bytes * 8`(ビットへの換算)に変えました。

| | SQLMesh | dbt |
|---|---|---|
| 判定 | silver は Breaking、gold は Indirect Breaking と表示 | 判定なし |
| 反映 | silver 6 区間と gold を作り直した | 次の定時実行で処理した 11 時だけが新しい式。**06〜10 時は古い式のまま、実行は成功** |

### 列の削除と型の変更

silver から `packets` 列(gold では使っていない)を消す変更と、`bytes` を `DOUBLE` にする変更です。dbt は [`on_schema_change`](https://docs.getdbt.com/docs/build/incremental-models) の 3 つの設定で試しました。

| 変更 | SQLMesh | dbt 既定(`ignore`) | dbt `fail` | dbt `sync_all_columns` |
|---|---|---|---|---|
| 列の削除 | Breaking と判定し全区間を作り直し | 失敗(データベースのエラー) | 失敗(Compilation Error) | 成功。列を削除したが、**`bytes` の型が BIGINT から INTEGER に変わった** |
| 型の変更 | Breaking と判定し全区間を作り直し | **成功するが型は BIGINT のまま、警告なし** | 失敗(Compilation Error) | 成功。`bytes` は DOUBLE になったが、**`packets` が BIGINT から INTEGER に変わった** |

`sync_all_columns` で INTEGER になった列は、今回のデータでは値が収まりました(`bytes` の最大は 165,045,979)。INTEGER の上限(2,147,483,647)を超える値が来ると入りません。1.12.5(Trino)の `sync_all_columns` では、触っていない列の型は変わりませんでした。

どの設定でも、dbt が計算し直したのは処理した区間だけで、過去の区間は計算し直されませんでした。

## ロールバック

式の変更を元に戻したときの挙動です。

| | SQLMesh | dbt |
|---|---|---|
| 方法 | モデルを元に戻して `plan` | モデルを元に戻して、全期間を範囲指定で流すか `--full-refresh` |
| 作り直した区間 | **0**。表示用の view を前の版の物理表に差し替えた | 全期間(6 区間) |

SQLMesh は、版ごとに物理表を作り、表示用の view をそこに向けます。前の版の物理表が残っているので、戻すときは view を差し替えるだけで済みました。dbt は、どちらの方法でも履歴を全部計算し直すので、履歴が長いほど時間がかかります。

## 履歴を持つ表(SCD Type 2)

小さな「担当チームの表」に、変更・削除・追加を 1 回ずつ入れました。SQLMesh は [`SCD_TYPE_2_BY_TIME`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/model_kinds.md)、dbt は [snapshot](https://docs.getdbt.com/docs/build/snapshots)(timestamp 方式)です。

| 場面 | SQLMesh | dbt |
|---|---|---|
| 最初の行の有効開始 | `1970-01-01` | 元の行の `updated_at` |
| 変更 | 旧行を変更時刻で閉じ、新行をその時刻から | 同じ |
| 削除 | 有効のまま(既定の `invalidate_hard_deletes: false`) | 有効のまま(既定の [`hard_deletes: ignore`](https://docs.getdbt.com/reference/resource-configs/hard-deletes)) |
| 後から追加した行の有効開始 | `1970-01-01` | その行の `updated_at` |

削除を記録しないのはどちらも既定の動作で、設定で変えられます。

## 外部データの到着を待つ

入力の 06〜08 時台だけを先に入れ、11 時台まで処理する実行のあとで、09〜11 時台を遅れて入れました。

SQLMesh は [signals](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/signals.md) で、区間ごとに「準備ができたか」を返す関数をモデルに付けられます。入力の最新時刻より前に終わる区間だけを返す関数を書きました。

```python
from sqlmesh import signal, DatetimeRanges, ExecutionContext

@signal()
def landed(batch: DatetimeRanges, context: ExecutionContext):
    newest = context.engine_adapter.fetchone(
        "SELECT epoch(max(time_flow_start)) FROM src.land")[0]
    if newest is None:
        return False
    return [(s, e) for s, e in batch if e.timestamp() <= newest]
```

| | 2 回目の実行の後(09〜11 時台が到着した後) |
|---|---|
| SQLMesh(signal あり) | 06〜10 時(11 時は 12:00 以降の行が到着するまで待つ) |
| SQLMesh(signal なし) | 06〜08 時だけ。1 回目に 09〜11 時を 0 行で「処理済み」にしたので、遅延到着データは入らない |
| dbt microbatch | 06〜08 時と 11 時。09・10 時は入らない |

dbt には、モデルの実行を到着まで止める仕組みがありません。[`dbt source freshness`](https://docs.getdbt.com/docs/deploy/source-freshness) は入力が古いことを検出しました(`Stale source src.land (last updated 2h 59m ago)`)が、`dbt run` はそれとは別に実行され、成功しました。

dbt 2.0 では、source の `loaded_at_field` を表の直下ではなく `config:` の中に書く必要がありました。1.x の書き方のままだと `Ignored unexpected key "loaded_at_field"` のエラーになり、同じプロジェクトの他のモデルやスナップショットも実行されませんでした。

## 変更の影響範囲

| | SQLMesh | dbt |
|---|---|---|
| 変更したモデルと下流 | `plan` が Breaking / Non-breaking と下流を表示 | [`state:modified+`](https://docs.getdbt.com/reference/node-selection/methods) で選べた。本番の manifest を保存しておく必要がある |
| テストによる関門 | [ユニットテスト](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/tests.md)が `plan` の前に自動で流れ、失敗すると `plan` を止めた | [unit tests](https://docs.getdbt.com/docs/build/unit-tests) は `dbt test` か `dbt build` で流す |
| dev 環境 | 変更したモデルだけを作り、上流は本番の表を共有 | プロファイルで別のスキーマを用意して作る(今回は未計測) |
| 本番との差分 | [`table_diff`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/tablediff.md) で、本番だけの行 294 行と一致した行 482 行を表示 | 差分の機能(compare changes)は [Enterprise プランのみ](https://docs.getdbt.com/docs/deploy/advanced-ci)で、自前の構成には無い |

## 状態 DB の障害とツールの更新

この節は、本番と同じ Trino の環境で SQLMesh 0.236.3(本番で使っているイメージ)を使って試しました。

SQLMesh の状態 DB の接続先を、存在しないポートに向けて実行しました。SQLMesh は 5 秒で接続エラー(`connection refused`)になり、何もせずに終了しました(終了コード 1)。表は変わっていません。安全側に止まりますが、状態 DB が止まると変換はすべて止まります。dbt には状態 DB が無いので、この種の影響はありません。

ツールの更新では、0.236.2 で作った状態 DB を 0.236.3 でそのまま使えました。この 2 つの版の間には状態 DB の移行がありません。移行が必要な版では、[公式のガイド](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/migrations.md)によると SQLMesh が `sqlmesh migrate` を求めて止まり、移行は 1 人が、誰も実行していないときに流す決まりです。

> Migration should be issued manually by a single user and the migration will affect all users of the project.

## 大量の再処理

6 時間分(197 万行)をまとめて処理し直しました。DuckDB では、どの方法も成功しました。

| 方法 | クエリ数 | 結果 |
|---|---:|---|
| SQLMesh 既定(区間をまとめて 1 クエリ) | 1 | 成功 |
| SQLMesh `batch_size 1`(1 時間ずつ並行) | 6 | 成功 |
| dbt microbatch | 6 | 成功 |
| dbt microbatch + [`concurrent_batches`](https://docs.getdbt.com/reference/resource-properties/concurrent_batches) | 6 | 成功 |

区間をまとめて 1 クエリで処理する SQLMesh の既定は、クエリが大きくなります。本番の Trino(1 ノード 1GB のメモリ上限)では、全期間を 1 クエリで作り直そうとしてメモリ上限を超えたことがあり、`batch_size` で分割して処理しました。

## Trino + Iceberg の環境でだけ起きたこと

本番と同じ Trino 483 + Iceberg(カタログは Lakekeeper、保存先は SeaweedFS)で、SQLMesh 0.236.2 / dbt-core 1.12.5 / dbt-trino 1.10.6 を使ったときにだけ起き、DuckDB では起きなかったことです。

| 場面 | 起きたこと |
|---|---|
| 区間ごとの再処理を並行に流す | SQLMesh(`batch_size 1`)も dbt(`concurrent_batches`)も、同じ Iceberg の表への削除どうしがぶつかって `ICEBERG_COMMIT_ERROR` で失敗した。SQLMesh は [`batch_concurrency`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md) を 1 にすると成功した |
| SQLMesh の作り直し(restate)が途中で失敗 | 状態 DB は「処理済み」のまま残り、次の普通の実行は何もしなかった。作り直しをもう一度流す必要がある |
| dbt の `--full-refresh` | 退避先の `<モデル名>__dbt_backup` が、Lakekeeper の soft-delete(削除した表の名前を 7 日残す設定)で残っていた名前とぶつかり失敗。gold は本体が消えて退避用の表と一時表だけが残った。範囲指定で流す方法は表の名前を変えないので問題なかった |
| SQLMesh の `table_diff` | `timestamp(6)` の列をキーにすると失敗した(`Requested timestamp precision '6' exceeds maximum supported precision: 3`) |
| SQLMesh のユニットテスト | タイムゾーン付きの時刻型の列を使うと、実行環境のタイムゾーンの影響を受けた。日本時間の端末では区間の境界がずれて失敗し、`TZ=UTC` で通った |
| Iceberg の保守 | どちらのツールも、小さなファイルをまとめる・古いスナップショットを消すことはしない。SQLMesh はロールバック用に前の版の物理表を残し、silver で 4 版(各 197 万行)が残った |

## まとめ

- 失敗した区間と飛んだ区間は、SQLMesh が状態 DB から次の実行で取り返しました。dbt 2.0.5 は、失敗したバッチの後ろのバッチも実行せず、`dbt retry` でも取り返せませんでした
- 式の変更は、SQLMesh が全期間を作り直し、dbt は処理した区間だけが新しい式になりました。ロールバックは SQLMesh が計算なし、dbt は全期間の計算し直しでした
- 上流の訂正は、どちらも普通の実行では拾わず、範囲指定で直りました。SQLMesh は下流まで自動で、dbt は `+` で下流を選びます
- 到着待ちは SQLMesh の signals で書けました。dbt の `source freshness` は検出だけで、実行は止めません
- SQLMesh は状態 DB が止まると全部止まります。Iceberg の上で区間ごとの再処理を並行に流すと、どちらも失敗しました

## 参考

- 検証時の構成ファイル: [sqlmesh(本番のモデル)](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/sqlmesh)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public)。環境固有値はダミーに置換済み)。本記事の検証用のモデルとスクリプトは未公開です

- [dbt: About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)
- [dbt: Configure incremental models(on_schema_change)](https://docs.getdbt.com/docs/build/incremental-models)
- [dbt: retry](https://docs.getdbt.com/reference/commands/retry)
- [dbt: concurrent_batches](https://docs.getdbt.com/reference/resource-properties/concurrent_batches)
- [dbt: Snapshots](https://docs.getdbt.com/docs/build/snapshots)
- [dbt: hard_deletes](https://docs.getdbt.com/reference/resource-configs/hard-deletes)
- [dbt: Source freshness](https://docs.getdbt.com/docs/deploy/source-freshness)
- [dbt: Unit tests](https://docs.getdbt.com/docs/build/unit-tests)
- [dbt: Node selector methods(state)](https://docs.getdbt.com/reference/node-selection/methods)
- [dbt: Advanced CI](https://docs.getdbt.com/docs/deploy/advanced-ci)
- [dbt: About dbt State](https://docs.getdbt.com/docs/deploy/dbt-state-about)
- [dbt v2: Supported features](https://docs.getdbt.com/docs/fusion/supported-features)
- [dbt-labs/dbt-core v2.0.5](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)
- [SQLMesh v0.236.3: Plans](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)
- [SQLMesh v0.236.3: Model kinds(SCD Type 2)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/model_kinds.md)
- [SQLMesh v0.236.3: Models overview(batch_concurrency)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md)
- [SQLMesh v0.236.3: Signals](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/signals.md)
- [SQLMesh v0.236.3: Tests](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/tests.md)
- [SQLMesh v0.236.3: Table diff](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/tablediff.md)
- [SQLMesh v0.236.3: Migrations](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/migrations.md)
- [DuckDB: Utility functions(error)](https://duckdb.org/docs/current/sql/functions/utility.html)
- [libfaketime](https://github.com/wolfcw/libfaketime)
