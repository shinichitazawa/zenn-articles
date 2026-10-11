---
title: "SQLMesh と dbt を失敗・訂正・ロールバックの運用で比べる"
emoji: "🔁"
type: "tech"
topics: ["sqlmesh", "dbt", "trino", "iceberg", "dataengineering"]
published: false
---

## はじめに

[前回の記事](https://zenn.dev/shinichitazawa/articles/039-sqlmesh-dbt-late-arrival-schema-change)では、SQLMesh と dbt を「遅れて届く行」と「列の追加」で比べました。本記事はその続きで、データパイプラインを運用していると起きる次の場面を、同じ環境で両方に再現して比べます。

- 実行が飛んだとき、一部の区間だけ失敗したとき
- 上流の過去データが後から訂正されたとき
- モデルの式・列・型を変えたとき、その変更を戻すとき
- 履歴を持つ表(SCD Type 2)、外部データの到着待ち、テスト、状態 DB の障害、ツールの更新、大量の再処理

結果は次のとおりです。

- **差が大きいのは「後から直す」場面でした。** SQLMesh は失敗した区間や飛んだ区間を状態 DB に記録していて、次の普通の実行で取り返しました。dbt は、人が範囲を指定して流すか `dbt retry` を使わない限り、空いた区間や新旧の式の混在が「成功」のまま残りました
- **ロールバックは、SQLMesh が計算なしで 3.2 秒、dbt は全期間を計算し直して 21.1 秒でした。** dbt の `--full-refresh` はこの環境では失敗し、表の本体が消えた状態で止まりました
- **SQLMesh には、状態 DB が止まると変換がすべて止まる、前の版の表のぶん容量を使う、という代償がありました**
- **上流の訂正を検知する機能と、Iceberg の保守(小さなファイルをまとめる)は、どちらにもありませんでした**

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.2(状態 DB の試験だけ 0.236.3) |
| dbt | dbt-core 1.12.5 / dbt-trino 1.10.6 |
| クエリエンジン | Trino 483 |
| テーブル | Iceberg(カタログは Lakekeeper、保存先は SeaweedFS) |

扱わないものは次のとおりです。

- 処理速度の比較。所要時間は参考として載せますが、1 回ずつの計測です
- オーケストレーター(Argo Workflows など)の比較
- dbt の次世代エンジン(Fusion)。前回の記事と同じく、Trino に未対応のためです

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 検証の方法

入力は、自宅クラスタの NetObserv(eBPF のフロー計測)のデータのうち、2026-10-04 06:00〜11:59 UTC の 1,970,669 行です。書き換えを試すため、専用の表に複製して使いました。

どちらのツールでも、silver(フロー 1 行ずつ)→ gold(時間・namespace ごとの集計)の 2 段のモデルを、1 時間の区間で作りました。

| | SQLMesh | dbt |
|---|---|---|
| silver | `INCREMENTAL_BY_TIME_RANGE`、`batch_size 1` | microbatch、`batch_size='hour'`、`lookback=1` |
| gold | `INCREMENTAL_BY_TIME_RANGE` | microbatch(silver に `event_time` を設定) |
| 実行時刻の与え方 | `sqlmesh run --end <時刻>` | 時計を [time-machine](https://github.com/adamchainz/time-machine) でずらして dbt を Python から呼び出す |

dbt の microbatch は、[公式ドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)のとおり、実行した時刻と `lookback` から処理する区間を決めます。外から区間を渡すと dbt 自身の判断を試せないので、時計だけをずらしました。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

失敗は、Trino の `fail()` 関数で 9 時台の行だけに起こしました。制御用の表に時間を書くと、その時間の行で `fail()` が呼ばれるようにしてあります。`fail()` は Trino の[公式の関数一覧](https://trino.io/docs/current/functions/list.html)には載っていない関数で、Trino 483 で動作することだけを確かめて使いました。

## 実行が飛んだとき

07:00 まで処理したあと、08〜10 時の実行を飛ばし、11:00 に実行しました(SQLMesh は lookback なし、dbt は lookback 1)。

| 時 | 入力 | SQLMesh | dbt |
|---|---:|---:|---:|
| 06 | 328,846 | 328,846 | 328,846 |
| 07 | 331,145 | 331,145 | 331,145 |
| 08 | 326,899 | 326,899 | 0 |
| 09 | 327,164 | 327,164 | 0 |
| 10 | 329,857 | 329,857 | 329,857 |
| 11 | 326,758 | 326,758 | 326,758 |

SQLMesh は、11:00 の実行で 07:00〜10:59 を 1 回のクエリでまとめて処理しました。dbt が処理したのは 10 時(lookback の分)と 11 時(実行時刻の区間)だけで、08・09 時は空いたまま実行が成功しました。空いた区間は、範囲を指定して流せば埋まりました。

```bash
dbt run --select gap_cfg_sql \
  --event-time-start "2026-10-04 08:00:00" --event-time-end "2026-10-04 10:00:00"
```

## 一部の区間だけ失敗したとき

9 時台だけを失敗させて 6 時間分を処理し、失敗の原因を取り除いてからもう一度実行しました。

| | SQLMesh | dbt |
|---|---|---|
| 1 回目の実行 | 9 時だけ失敗。成功した 5 区間は状態 DB に記録され、gold は上流が揃わないので丸ごと保留 | 9 時のバッチだけ失敗(`Completed with 1 partial success`)。gold は SKIP |
| 2 回目(原因を除いた後) | 普通の `sqlmesh run` で 9 時だけを処理し、そのあと gold を作った | `dbt retry` で 9 時だけを処理し、そのあと gold を作った |
| `dbt retry` を使わず次の定時実行を迎えた場合 | — | 11・12 時だけを処理して成功。**9 時は silver・gold とも 0 行のまま** |

どちらも失敗した区間だけをやり直せますが、覚えている場所が違います。[`dbt retry`](https://docs.getdbt.com/reference/commands/retry) は前回の実行結果のファイル(`target/run_results.json`)を読みます。実行のたびにコンテナを作り直す環境では、このファイルを保存しておかない限り次の実行で使えません。SQLMesh は状態 DB に記録しているので、次の普通の実行で取り返せました。

### 作り直し(restate)が途中で失敗したとき

SQLMesh の作り直し(`plan --restate-model`)が途中で失敗したときは、状態 DB は「処理済み」のまま残りました。次の普通の実行は「処理すべき区間が無い」と答えるので、作り直しはもう一度実行する必要があります。データは元のまま残っていました。Iceberg の書き込みは区間ごとに確定するので、失敗した区間には何も書かれていなかったためです。

このときの失敗の原因は、注入した `fail()` ではなく Iceberg のコミットの競合でした。後の「大量の再処理」の節で扱います。

## 上流の過去データが訂正されたとき

入力の 8 時台の `bytes` を 0 に書き換えて、上流で過去のデータが訂正された状況を作りました。

| 操作 | SQLMesh | dbt |
|---|---|---|
| 普通の実行 | 拾わない(silver・gold とも元の値) | 拾わない |
| 範囲を指定した再処理 | `plan --restate-model ops_sm.silver --start … --end …` で silver が直り、**gold も自動で作り直された** | `--event-time-start/--end` で直る。gold まで直すには選択に `+`(`--select ops_silver+`)が要る |
| 同じ範囲を 2 回流す | 件数・値とも変わらない | 件数・値とも変わらない |

SQLMesh の作り直しが下流に伝わるのは、[plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/plans.md)のとおりでした。

> Applying a restatement plan will trigger a cascading backfill for all selected models, as well as all models downstream from them.

dbt の microbatch は区間ごとに「消してから入れ直す」ので、同じ区間を何度流しても重複しません。生成された SQL も、区間ごとに `delete … where time_flow_start >= 区間の始め and < 区間の終わり` を実行してから `insert` していました。

## モデルを変えたとき

### 式の変更

silver の `bytes` を `bytes * 8`(ビットへの換算)に変えました。

| | SQLMesh | dbt |
|---|---|---|
| 判定 | silver は Breaking、gold は Indirect Breaking と表示 | 判定なし |
| 反映 | 6 時間分の silver と gold を作り直した(7.9 秒) | 次の定時実行で処理した 11 時だけが新しい式。**06〜10 時は古い式のまま、実行は成功** |

### 列の削除と型の変更

silver から `packets` 列(gold では使っていない)を消す変更と、`bytes` を `double` にする変更です。dbt は [`on_schema_change`](https://docs.getdbt.com/docs/build/incremental-models) の 3 つの設定で試しました。

| 変更 | SQLMesh | dbt 既定(`ignore`) | dbt `fail` | dbt `sync_all_columns` |
|---|---|---|---|---|
| 列の削除 | Breaking と判定し全期間を作り直し(7.6 秒) | 失敗(Trino が列を見つけられない) | 失敗(差分を表示) | 成功。列を表から削除 |
| 型の変更 | Breaking と判定し全期間を作り直し(7.9 秒) | **成功するが型は bigint のまま、警告なし** | 失敗 | 成功。列の型を変更(列の順番が変わった) |

dbt では、どの設定でも計算し直されるのは処理した区間だけで、過去の区間は計算し直されませんでした。

## ロールバック

式の変更を元に戻したときの挙動です。

| | SQLMesh | dbt |
|---|---|---|
| 方法 | モデルを元に戻して `plan` | モデルを元に戻して全期間を範囲指定で流す |
| 計算 | なし。表示用の view を前の版の物理表に差し替えた | 6 時間分を計算し直した |
| 所要 | 3.2 秒 | 21.1 秒 |

SQLMesh は、版ごとに物理表を作り、表示用の view をそこに向けます。前の版の物理表が残っているので、戻すときは view を差し替えるだけで済みました。この差は、履歴が長いほど開きます。

dbt の `--full-refresh` は、この環境では使えませんでした。作り直しの途中で古い表を `<モデル名>__dbt_backup` という決まった名前に退避しますが、Lakekeeper は削除した表の名前を 7 日間残す設定(soft-delete)にしてあり、前に使った名前とぶつかって `Error getting tabular from catalog` で失敗しました。gold では、**本体が消えて退避用の表と一時表だけが残った状態**で止まりました。範囲指定で流す方法は表の名前を変えないので、この問題は起きませんでした。

## 履歴を持つ表(SCD Type 2)

小さな「担当チームの表」に、変更・削除・追加を 1 回ずつ入れました。SQLMesh は [`SCD_TYPE_2_BY_TIME`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/model_kinds.md)、dbt は [snapshot](https://docs.getdbt.com/docs/build/snapshots)(timestamp 方式)です。

| 場面 | SQLMesh | dbt |
|---|---|---|
| 最初の行の有効開始 | `1970-01-01` | 元の行の `updated_at` |
| 変更 | 旧行を変更時刻で閉じ、新行をその時刻から | 同じ |
| 削除 | 有効のまま(既定の `invalidate_hard_deletes: false`) | 有効のまま(既定の [`hard_deletes: ignore`](https://docs.getdbt.com/reference/resource-configs/hard-deletes)) |
| 後から追加した行の有効開始 | `1970-01-01` | その行の `updated_at` |

削除を記録しないのはどちらも既定の動作で、設定で変えられます。違いは有効開始の決め方で、SQLMesh は初めて現れた行を「最初から有効」として扱いました。

## 外部データの到着を待つ

入力の 06〜08 時台だけを先に入れ、11 時台まで処理する実行のあとで、09〜11 時台を遅れて入れました。

SQLMesh は [signals](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/guides/signals.md) で、区間ごとに「準備ができたか」を返す関数をモデルに付けられます。入力の最新時刻より前に終わる区間だけを返す関数を書きました。

```python
from sqlmesh import signal, DatetimeRanges, ExecutionContext

@signal()
def landed(batch: DatetimeRanges, context: ExecutionContext):
    newest = context.engine_adapter.fetchone(
        "SELECT to_unixtime(max(time_flow_start)) FROM lakehouse.cmp_src.ops_land")[0]
    if newest is None:
        return False
    return [(s, e) for s, e in batch if e.timestamp() <= newest]
```

| | 1 回目(入力は 08 時台まで) | 09〜11 時台が届いた後の 2 回目 |
|---|---|---|
| SQLMesh(signal あり) | 06・07 時だけ処理し、08 時以降は待った | 08〜10 時を処理(11 時は 12:00 の行が届くまで待つ) |
| SQLMesh(signal なし) | 06〜11 時をすべて処理済みにした(09〜11 時は 0 行) | 何もしない。遅れて届いた行は入らない |
| dbt microbatch | 06〜11 時を処理(09〜11 時は 0 行) | lookback で 11 時だけ拾い、09・10 時は入らない |

dbt には、モデルの実行を到着まで止める仕組みがありません。[`dbt source freshness`](https://docs.getdbt.com/docs/deploy/source-freshness) は入力が古いことを検出しました(`ERROR STALE`)が、`dbt run` はそれとは別に実行され、成功しました。dbt で到着を待つには、オーケストレーター側で鮮度の確認を前段に置く必要があります。

## テストと変更の影響範囲

### ユニットテスト

gold の集計(1 時間ごとの `bytes` の合計)に、同じ内容のテストを書きました。

| | SQLMesh([tests](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/tests.md)) | dbt([unit tests](https://docs.getdbt.com/docs/build/unit-tests)) |
|---|---|---|
| 実行先 | DuckDB(手元。ウェアハウスに接続しない) | Trino |
| 所要 | 2.9 秒 | 5.0 秒 |
| 変更の前に自動で流れるか | `plan` の前に流れ、失敗すると `plan` を止めた | `dbt test` か `dbt build` で流す |
| 注意した点 | 実行環境のタイムゾーンの影響を受けた。日本時間の端末では区間の境界がずれて失敗し、`TZ=UTC` で通った | microbatch のモデルでは `is_incremental` の上書きが要った。時刻を `TIMESTAMP '…'` の形で書くと NULL になり、文字列で書くと通った |

### 変更の影響範囲

| | SQLMesh | dbt |
|---|---|---|
| 変更したモデルと下流 | `plan` が Breaking / Non-breaking と下流を表示 | [`state:modified+`](https://docs.getdbt.com/reference/node-selection/methods) で選べた。本番の manifest を保存しておく必要がある |
| dev 環境 | 変更したモデルだけを作り、上流は本番の表を共有(5.8 秒) | プロファイルで別のスキーマを用意して作る(今回は未計測) |
| 本番との差分 | [`table_diff`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/guides/tablediff.md)。**時刻の列をキーにすると Trino で失敗**(`Requested timestamp precision '6' exceeds maximum supported precision: 3`)。キーから外すと動き、本番だけの行 49 行と一致した行 83 行を表示 | 差分の機能(compare changes)は [Enterprise プランのみ](https://docs.getdbt.com/docs/deploy/advanced-ci)で、対応するデータベースに Trino は無い(2026-10-10 時点) |

## 状態 DB の障害とツールの更新

SQLMesh の状態 DB の接続先を、存在しないポートに向けて実行しました。SQLMesh は 5 秒で接続エラー(`connection refused`)になり、何もせずに終了しました(終了コード 1)。表は変わっていません。安全側に止まりますが、状態 DB が止まると変換はすべて止まります。dbt には状態 DB が無いので、この種の影響はありません。

ツールの更新では、0.236.2 で作った状態 DB を 0.236.3 でそのまま使えました。状態 DB に記録された版(`schema_version 102`)も変わらず、0.236.2 に戻しても動きました。この 2 つの版の間には状態 DB の移行がありません。移行が必要な版では、[公式のガイド](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/migrations.md)によると SQLMesh が `sqlmesh migrate` を求めて止まり、移行は 1 人が、誰も実行していないときに流す決まりです。

> Migration should be issued manually by a single user and the migration will affect all users of the project.

## 大量の再処理と並行実行

6 時間分(197 万行)をまとめて処理し直しました。

| 方法 | クエリ数 | 所要 | 結果 |
|---|---:|---:|---|
| SQLMesh 既定(区間をまとめて 1 クエリ) | 1 | 5.7 秒 | 成功 |
| SQLMesh `batch_size 1`(1 時間ずつ並行) | 6 | — | **3 回とも失敗**(Iceberg のコミットの競合) |
| SQLMesh `batch_size 1` + `batch_concurrency 1` | 6 | 15.5 秒 | 成功 |
| dbt microbatch | 6 | 12.9 秒 | 成功 |
| dbt microbatch + [`concurrent_batches`](https://docs.getdbt.com/reference/resource-properties/concurrent_batches) | 6 | 9.5 秒 | **失敗**(`ICEBERG_COMMIT_ERROR`) |

区間ごとの「消してから入れ直す」を、同じ Iceberg の表に並行して流すと、どちらのツールでも削除どうしがぶつかって失敗しました。初回の処理(追記だけ)は並行でも成功しています。SQLMesh の [`batch_concurrency`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/overview.md) を 1 にすると成功しました。この設定の変更は「Metadata Updated」と判定され、作り直しは起きませんでした。

区間をまとめて 1 クエリで処理する SQLMesh の既定は速い反面、本番では 1 クエリが大きくなりすぎて Trino のメモリ上限(1 ノード 1GB)を超えたことがあります。そのときは `batch_size` で分割して処理しました。

### Iceberg の保守

どちらのツールも、Iceberg の表の保守(小さなファイルをまとめる、古いスナップショットを消す)はしません。今回の試験のあと、SQLMesh の silver の現在の版には 40 ファイル、dbt の範囲指定を繰り返した表には 32 ファイルが溜まっていました。

SQLMesh は、ロールバックのために前の版の物理表を残します。silver には 4 つの版(各 197 万行)が残っていました。古い版は、期限が来ると後片付け(janitor)が消します。

## この環境で見つかった制約

比較とは別に、この環境に固有の制約が 2 つありました。

- **Lakekeeper の soft-delete と、決まった名前を使い回す処理がぶつかります。** dbt の `--full-refresh` の退避先(`__dbt_backup`)と、SQLMesh の janitor が消そうとする空の dev スキーマが該当しました
- **SQLMesh の `table_diff` は、`timestamp(6)` の列をキーにすると Trino で失敗しました**

## まとめ

- 失敗した区間と飛んだ区間は、SQLMesh が状態 DB から次の実行で取り返しました。dbt は `dbt retry`(前回の実行結果のファイルが必要)か範囲指定で取り返し、どちらも使わないと空いたまま成功しました
- 式の変更は、SQLMesh が全期間を作り直し、dbt は処理した区間だけが新しい式になりました。ロールバックは SQLMesh が計算なしで 3.2 秒、dbt は全期間の計算し直しで 21.1 秒でした
- 上流の訂正は、どちらも普通の実行では拾わず、範囲指定で直りました。SQLMesh は下流まで自動で、dbt は `+` で下流を選びます
- 到着待ちは SQLMesh の signals で書けました。dbt の `source freshness` は検出だけで、実行は止めません
- SQLMesh は状態 DB が止まると全部止まり、前の版の表のぶん容量を使います。区間ごとの再処理を並行に流すと、どちらも Iceberg のコミットの競合で失敗しました

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
- [SQLMesh v0.236.2: Plans](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/plans.md)
- [SQLMesh v0.236.2: Model kinds(SCD Type 2)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/model_kinds.md)
- [SQLMesh v0.236.2: Models overview(batch_concurrency)](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/models/overview.md)
- [SQLMesh v0.236.2: Signals](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/guides/signals.md)
- [SQLMesh v0.236.2: Tests](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/concepts/tests.md)
- [SQLMesh v0.236.2: Table diff](https://github.com/SQLMesh/sqlmesh/blob/v0.236.2/docs/guides/tablediff.md)
- [SQLMesh v0.236.3: Migrations](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/migrations.md)
- [Trino: List of functions and operators](https://trino.io/docs/current/functions/list.html)
- [time-machine](https://github.com/adamchainz/time-machine)
