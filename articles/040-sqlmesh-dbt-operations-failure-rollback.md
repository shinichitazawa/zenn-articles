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

- **差が大きいのは、失敗や変更を後から直す場面でした。** SQLMesh は処理し終えた区間を状態 DB に記録していて、失敗した区間や飛んだ区間を次の普通の実行で処理し直しました。dbt は、人が範囲を指定して流さない限り、空いた区間や新旧の式の混在が残ったまま実行が成功しました
- **dbt 2.0.5 では、区間の処理(バッチ)が 1 つ失敗すると後ろのバッチも実行されず、`dbt retry` でも処理し直せませんでした。** 実行結果のファイルに、失敗したモデルが記録されていなかったためです
- **ロールバックは、SQLMesh が計算なし(作り直した区間 0)、dbt は全期間の計算し直しでした**
- **SQLMesh には、状態 DB が止まると変換がすべて止まる、前の版の表のぶん容量を使う、という代償がありました**

| 項目 | 版・構成 |
|---|---|
| SQLMesh | 0.236.3 |
| dbt | 2.0.5(Rust で書き直された版。[リリース](https://github.com/dbt-labs/dbt-core/releases/tag/v2.0.5)) |
| エンジン | DuckDB 1.5.6 |
| 入力 | 本番のフローの記録 1,970,669 行 |

入力は、本番から書き出したネットワークのフローの記録で、フローの開始時刻が 2026-10-04 06:00:00〜11:59:59 UTC のものです。版は 2026-10-11 時点のそれぞれの最新の安定版です。本番のエンジンは Trino ですが、dbt 2.0 の[対応表](https://docs.getdbt.com/docs/fusion/supported-features)に Trino が無いので、両方が動く DuckDB で比べました。本番と同じ Trino + Iceberg の上でも、1 つ前の組み合わせ(SQLMesh 0.236.2 / dbt-core 1.12.5 / dbt-trino 1.10.6)で同じ試験をしており、エンジンに固有の結果は後の節に分けて書きます。

扱わないものは次のとおりです。

- 遅延到着データ。本記事の試験は、入力の全行が最初から揃った状態で行いました(遅延到着は前回の記事で扱いました)
- 処理速度の比較。DuckDB では 197 万行の処理がどれも数秒で終わるので、所要時間ではなく「どの区間を計算し直したか」で比べます
- dbt platform の機能。dbt 2.0 の `dbt State` は、[dbt platform のアカウントで動き、最終更新時刻と SQL のハッシュを dbt Labs のサーバーに送る](https://docs.getdbt.com/docs/deploy/dbt-state-about)機能なので、自前で動かす構成の比較から外しました
- オーケストレーター(Argo Workflows など)の比較

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実は筆者が公式ドキュメントを引用して検証していますが、誤りや改善点があればコメント等でご指摘ください。
:::

## 前提: 2 つのツールの増分処理

どちらのツールも、毎回すべてを作り直さず、新しく増えた分だけを処理する増分処理を使います。単位は、時刻の列(ここではフローの開始時刻 `time_flow_start`)で 1 時間ごとに区切った「区間」です。

| | SQLMesh | dbt |
|---|---|---|
| 増分処理の種類 | `INCREMENTAL_BY_TIME_RANGE` | microbatch |
| 処理する区間 | まだ処理していない区間 | 実行時刻の区間と、その前の `lookback` 個 |
| 処理済みの区間の記録 | 状態 DB に記録する | 記録しない |
| 1 区間の処理 | 区間ごとに書き込む | 区間ごとに消して入れ直す |
| 変更を本番に入れる手順 | `sqlmesh plan` で確認して適用 | 無い(`dbt run` で反映) |

- microbatch は、dbt の incremental モデルに `incremental_strategy='microbatch'` を設定して使います
- 状態 DB は SQLMesh 専用のデータベースで、モデルの版と処理済みの区間を記録します(今回は DuckDB、本番は PostgreSQL)。SQLMesh はこの記録と照らして、まだ処理していない区間を選びます
- dbt は記録を持たないので、実行のたびに、実行した時刻から区間を計算します
- 今回の SQLMesh は `batch_size 1` で 1 区間ずつ書き込みました。dbt は 1 区間を 1 クエリで処理し、これをバッチと呼びます
- `sqlmesh plan` は、変更の種類と作り直す区間を表示し、確認してから適用します。dbt は `dbt run` のときに、そのときのモデルの SQL をそのまま使います

dbt の microbatch の区間の決め方は、[公式ドキュメント](https://docs.getdbt.com/docs/build/incremental-microbatch)に次のとおり書かれています。

> During standard incremental runs, dbt will process batches according to the current timestamp and the configured `lookback`, with one query per batch.

## 検証の方法

どちらのツールでも、silver(フロー 1 行ずつ)→ gold(時間・namespace ごとの集計)の 2 段のモデルを作りました。gold は silver を入力にするので、silver の下流にあたります。

| | SQLMesh | dbt |
|---|---|---|
| silver | `INCREMENTAL_BY_TIME_RANGE`、`batch_size 1` | microbatch、1 時間の区間、`lookback=1` |
| gold | `INCREMENTAL_BY_TIME_RANGE` | microbatch |
| 実行した時刻の与え方 | `sqlmesh run --end <時刻>` | libfaketime で時計をずらす |

- dbt の gold は、silver の時刻の列を `event_time` に設定しました
- `sqlmesh run --end <時刻>` は、その時刻までに終わった区間を処理します
- dbt 2.0.5 には実行時刻を外から与えるオプションが無いので、[libfaketime](https://github.com/wolfcw/libfaketime) で dbt から見える時計をずらしました

失敗は、DuckDB の [`error()`](https://duckdb.org/docs/current/sql/functions/utility.html) 関数(与えたメッセージでエラーを起こす関数)で、9 時台の行だけに起こしました。制御用の表に時間を書くと、その時間の行で `error()` が呼ばれるようにしてあります。

## 実行が飛んだとき

**SQLMesh は、飛ばした区間を次の実行で埋めました。dbt は、飛ばした 08・09 時台を 0 行のまま残し、実行は成功しました。**

初回を実行したあと、毎時の実行を 3 回(08〜10 時)飛ばし、11 時に実行しました。実行時刻は、SQLMesh が 07:00 と 11:00(`--end` で指定)、dbt が 07:05 と 11:05 です。dbt は実行時刻を含む区間も処理するので、11 時台に入る 11:05 にしました。表は、2 回目の実行の後の silver の行数を、フローの開始時刻の区間ごとに数えたものです。

| 区間(UTC) | 入力の行数 | SQLMesh の silver の行数 | dbt の silver の行数 |
|---|---:|---:|---:|
| 06 時台 | 328,846 | 328,846 | 328,846 |
| 07 時台 | 331,145 | 331,145 | 331,145 |
| 08 時台 | 326,899 | 326,899 | **0** |
| 09 時台 | 327,164 | 327,164 | **0** |
| 10 時台 | 329,857 | 329,857 | 329,857 |
| 11 時台 | 326,758 | 0(未処理) | 326,758 |

- SQLMesh は 11:00 の実行で、状態 DB と照らして、まだ処理していない 07〜10 時台の 4 区間を処理しました。11 時台は終わっていない区間なので処理しません
- dbt は 11:05 の実行で、実行時刻の区間(11 時台)と、`lookback=1` による 1 つ前の区間(10 時台)だけを処理しました。06・07 時台は初回(07:05)の実行で処理済みです
- dbt の 07・11 時台に全行が入ったのは、この試験では入力の全行が最初から揃っているためです。本番では、実行時刻の区間には、実行した時点までに届いた行だけが入ります

dbt で空いた区間を埋めるには、範囲を指定して流します。

```bash
dbt run --select ops_silver+ \
  --event-time-start "2026-10-04 08:00:00" --event-time-end "2026-10-04 10:00:00"
```

## 一部の区間だけ失敗したとき

**SQLMesh は、失敗した 9 時台だけを次の普通の実行で処理し直しました。dbt 2.0.5 は、失敗したバッチの後ろのバッチも実行せず、`dbt retry` の後も silver の 9〜11 時台が 0 行のままでした。**

9 時台だけを失敗させて 06〜11 時台を処理し、失敗の原因を取り除いてからもう一度実行しました。

| | SQLMesh 0.236.3 | dbt 2.0.5 |
|---|---|---|
| 1 回目の silver | 9 時台だけ失敗 | 9 時台で失敗し、**10・11 時台は実行されない** |
| 1 回目の gold | 処理されない | スキップ |
| 失敗の記録 | 成功した 5 区間を状態 DB に記録 | **失敗した silver の記録が無い** |
| 2 回目(原因を除いた後) | 9 時台を処理し、gold を作る | **silver の 9〜11 時台は 0 行のまま** |
| retry せず次の定時実行(12:05) | — | **9・10 時台は 0 行のまま** |

- SQLMesh の 1 回目は 9 時台だけが失敗し、gold は上流の silver が揃わないので処理されませんでした。2 回目は普通の `sqlmesh run` で 9 時台だけを処理し、そのあと gold を作りました
- dbt の 1 回目は、silver を 06〜08 時台まで処理したところで 9 時台が失敗し、10・11 時台のバッチは実行されませんでした。gold は上流の失敗でスキップされました。実行結果のファイル `run_results.json`(下で説明します)には gold の「スキップ」1 件だけがあり、失敗した silver の記録はありませんでした
- dbt の 2 回目は `dbt retry` で、gold だけを作り直して成功しました。silver の 9〜11 時台は 0 行のままで、gold もその状態で作られました
- retry せずに次の定時実行(12:05)を迎えた場合は、11・12 時台だけを処理して成功しました

`run_results.json` は、dbt が実行のたびに書き出す、モデルごとの結果(成功・失敗・スキップ)のファイルです。[`dbt retry`](https://docs.getdbt.com/reference/commands/retry) はこのファイルを読み、失敗した箇所からやり直します。

> Retry re-executes the last invocation from the point of failure.

dbt 2.0.5 では、このファイルに失敗した silver が記録されていなかったので、`dbt retry` は silver をやり直しませんでした。2 回試して同じ結果でした。1 つ前の dbt-core 1.12.5(Trino)では、失敗したバッチを飛ばして 10・11 時台を処理し(`Completed with 1 partial success`)、`dbt retry` で 9 時台だけを処理し直せました。dbt-core の issue を検索しましたが、該当するものは見つかりませんでした(2026-10-11 時点)。

また、`run_results.json` は実行した場所のファイルなので、実行のたびにコンテナを作り直す環境では、保存しておかない限り次の `dbt retry` で使えません。SQLMesh は処理済みの区間を状態 DB に記録しているので、次の普通の実行で処理し直せました。

## 上流の過去データが訂正されたとき

**どちらも普通の実行では訂正を拾わず、範囲を指定した作り直しで直りました。下流の gold まで自動で作り直したのは SQLMesh で、dbt は選択に `+` を付けたときだけでした。**

入力の 8 時台の `bytes` をすべて 0 に書き換えて、上流で過去のデータが訂正された状況を作りました。silver と gold の 8 時台の `bytes` の合計が、入力と一致するかを見ました。

| 操作 | SQLMesh | dbt |
|---|---|---|
| 普通の実行 | 訂正を拾わない | 訂正を拾わない |
| 範囲を指定した作り直し | 直る。**gold も自動で作り直す** | 直る。gold は `+` で選んだときだけ |
| 同じ範囲を 2 回流す | 重複しない | 重複しない |

- 普通の実行では、どちらも silver・gold とも古い値のままでした
- SQLMesh は `sqlmesh plan --restate-model sm.ops_silver --start … --end …` で silver が直り、gold も自動で作り直されました
- dbt は `dbt run --event-time-start … --event-time-end …` で直りました。silver だけを選ぶと gold は古い値のままで、gold まで直すには選択に `+`(`--select ops_silver+`)を付けます
- 同じ範囲を 2 回流しても、どちらも件数・値とも変わりませんでした

SQLMesh では、処理済みの区間を作り直すことを restatement と呼びます([plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md))。作り直しが下流に伝わるのも、同じページの説明どおりでした。

> Applying a restatement plan will trigger a cascading backfill for all selected models, as well as all models downstream from them.

dbt の microbatch は区間のデータを消してから入れ直すので、同じ区間を何度流しても重複しません。上流の訂正を検知する機能は、どちらにもありませんでした。

## モデルを変えたとき

**SQLMesh は変更を分類し、新しい定義で過去の区間も作り直しました。dbt は、変更の後に処理した区間だけに新しい定義を使い、過去の区間は作り直しませんでした。**

SQLMesh は、`plan` のときに変更を分類して表示します。[plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)によると、Breaking は変更したモデルと下流のモデルを作り直す変更です。

> If a directly modified model change is categorized as breaking, it and its downstream dependencies will be backfilled.

変更したモデルの下流は「Indirect(間接的に変更された)」と表示されます。dbt には、変更を分類する仕組みはありません。

### 式の変更

**SQLMesh では全期間が新しい式になりました。dbt は 11 時台だけが新しい式になり、06〜10 時台は古い式のまま実行が成功しました。**

silver の `bytes` を `bytes * 8`(バイトからビットへの換算)に変えました。dbt は変更後に次の定時実行(12:05)を迎えた状態、SQLMesh は `plan` で適用した状態です。

| | SQLMesh | dbt |
|---|---|---|
| 変更の分類 | silver は Breaking、gold は Indirect Breaking | なし |
| 反映 | 全期間が新しい式 | 11 時台だけが新しい式。**06〜10 時台は古い式のまま** |

SQLMesh は silver の 6 区間と gold を作り直しました。dbt は 12:05 の実行で処理した 11 時台だけが新しい式になり、実行は成功しました。

### 列の削除と型の変更

**SQLMesh は、どちらの変更も全区間を作り直しました。dbt は `on_schema_change` の設定によって、失敗するか、警告なしに表の型が変わらないか、変えていない列の型が変わりました。**

silver から `packets` 列(gold では使っていない列)を消す変更と、`bytes` の型を BIGINT から DOUBLE にする変更です。dbt は、モデルの列と既存の表の列が違うときの動作を決める設定 [`on_schema_change`](https://docs.getdbt.com/docs/build/incremental-models) の 3 つの値で試しました。

| 変更 | SQLMesh | dbt `ignore`(既定) | dbt `fail` | dbt `sync_all_columns` |
|---|---|---|---|---|
| 列の削除 | 全区間を作り直す | 失敗 | 失敗 | 成功。**変えていない `bytes` が INTEGER に** |
| 型の変更 | 全区間を作り直す | 成功。**表の型は BIGINT のまま** | 失敗 | 成功。**変えていない `packets` が INTEGER に** |

- SQLMesh は、どちらの変更も Breaking と判定し、全区間を作り直しました
- `ignore` の列の削除は、データベースのエラーで失敗しました(Trino で試したときは `Column 'packets' cannot be resolved`)。型の変更は成功しましたが、表の型は BIGINT のままで、警告はありませんでした
- `fail` は、dbt が列の違いを検出し、Compilation Error で止まりました
- `sync_all_columns` は、列の削除では列を表から削除しましたが、`bytes` の型が BIGINT から INTEGER に変わりました。型の変更では `bytes` が DOUBLE になりましたが、変えていない `packets` が BIGINT から INTEGER に変わりました

`sync_all_columns` で INTEGER になった列は、今回のデータでは値が収まりました(`bytes` の最大は 165,045,979)。INTEGER の上限(2,147,483,647)を超える値が来ると書き込めません。1 つ前の dbt-core 1.12.5(Trino)の `sync_all_columns` では、変えていない列の型は変わりませんでした。

dbt では、どの設定でも計算し直されるのは処理した区間だけで、過去の区間は計算し直されませんでした。

## ロールバック

**SQLMesh は前の版の表に戻すだけで、作り直した区間は 0 でした。dbt は全期間を計算し直しました。**

上の「式の変更」を元に戻しました。

| | SQLMesh | dbt |
|---|---|---|
| 方法 | モデルを元に戻して `sqlmesh plan` | モデルを元に戻して、全期間を流し直す |
| 作り直した区間 | **0** | 全期間(6 区間) |

dbt で全期間を流し直す方法は、範囲を指定した `dbt run` か `dbt run --full-refresh` です。

SQLMesh は、モデルを変えるたびに新しい版の物理表を作り、本番の名前の view をその物理表に向けます。[plans の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/plans.md)には次のとおり書かれています。

> Every time a model is changed as part of a plan, a new variant of this model gets created behind the scenes (a snapshot with a unique fingerprint is assigned to it). In turn, each model variant's data is stored in a separate physical table.

前の版の物理表が残っているので、元に戻すときは view を前の版の物理表に向け直すだけで済みました。dbt は表を 1 つしか持たないので、どちらの方法でも履歴を全部計算し直します。この差は履歴が長いほど開きます。

ただし、`--forward-only`(既存の物理表を使い続け、作り直さない変更)で入れた変更は、版ごとの物理表を作らないので、同じ方法では戻せません。同じページには次のとおり書かれています。

> The drawback is that reverting to a previous version is no longer simple and requires a combination of additional forward-only changes and restatements.

## 履歴を持つ表(SCD Type 2)

**値の変更と削除の扱いは同じでした。違ったのは有効開始で、SQLMesh は初めて現れた行を `1970-01-01` から有効として扱いました。**

SCD Type 2 は、行の値が変わるたびに古い行を残し、新しい行を足して、各行が有効だった期間(有効開始・有効終了)を持たせる表の作り方です。[SQLMesh の説明](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/model_kinds.md)では次のとおりです。

> The `valid_from` column is the timestamp that the record became valid (inclusive) and the `valid_to` column is the timestamp that the record became invalid (exclusive).

小さな「担当チームの表」(id、担当、更新時刻)に、変更・削除・追加を 1 回ずつ入れました。SQLMesh は `SCD_TYPE_2_BY_TIME`、dbt は [snapshot](https://docs.getdbt.com/docs/build/snapshots)(更新時刻の列で変化を見る timestamp 方式)です。

| 場面 | SQLMesh | dbt |
|---|---|---|
| 最初からある行の有効開始 | `1970-01-01` | 元の行の更新時刻 |
| 担当を変えた行 | 旧行を変更時刻で終え、新行をその時刻から有効にする | 同じ |
| 削除した行 | 有効のまま | 有効のまま |
| 後から追加した行の有効開始 | `1970-01-01` | その行の更新時刻 |

削除した行が有効のままなのは、どちらも既定の設定(SQLMesh の `invalidate_hard_deletes: false`、dbt の [`hard_deletes: ignore`](https://docs.getdbt.com/reference/resource-configs/hard-deletes))によるもので、設定で変えられます。

## 外部データの到着を待つ

**SQLMesh は signals で、入力が揃うまで区間の処理を待たせられました。dbt には実行を止める仕組みが無く、`dbt source freshness` は入力の古さを検出するだけでした。**

入力の 06〜08 時台だけを先に入れて 11 時台まで処理する実行をしたあと、09〜11 時台を入れて、もう一度実行しました。上流のデータがそろう前に区間を処理済みにしてしまうと、あとから入ったデータを取りこぼします。

SQLMesh は [signals](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/signals.md) という仕組みで、区間ごとに「処理してよいか」を返す関数をモデルに付けられます。入力の最新時刻より前に終わる区間だけを返す関数を書きました。

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

| | 2 回目の実行の後に入っていた区間 |
|---|---|
| SQLMesh(signal あり) | 06〜10 時台 |
| SQLMesh(signal なし) | 06〜08 時台 |
| dbt microbatch | 06〜08 時台と 11 時台 |

- signal ありでは、11 時台は 12:00 以降の行が入るまで処理を待ちます
- signal なしでは、1 回目に 09〜11 時台を 0 行のまま処理済みにしたので、あとから入った行は入りませんでした

dbt には、モデルの実行を入力の到着まで止める仕組みがありません。[`dbt source freshness`](https://docs.getdbt.com/docs/deploy/source-freshness)(入力の最終更新時刻を確かめるコマンド)は入力が古いことを検出しました(`Stale source src.land (last updated 2h 59m ago)`)が、`dbt run` はそれとは別に実行され、成功しました。

dbt 2.0 では、source の `loaded_at_field`(鮮度を測る時刻の列)を、表の直下ではなく `config:` の中に書く必要がありました。1.x の書き方のままだと `Ignored unexpected key "loaded_at_field"` のエラーになり、同じプロジェクトの他のモデルやスナップショットも実行されませんでした。

## 変更の影響範囲

**SQLMesh は、変更の影響の表示、変更前のテスト、本番との差分の比較を `plan` と標準のコマンドで行えました。dbt は、影響の特定に本番の manifest の保存が要り、表の差分の比較は Enterprise プランだけの機能でした。**

変更を本番に入れる前に、どこまで影響するかを確かめる機能を比べました。

| | SQLMesh | dbt |
|---|---|---|
| 変更したモデルと下流の特定 | `plan` が表示する | `state:modified+` で選べる |
| 変更前のテスト | `plan` の前に自動で流れる | `dbt test` か `dbt build` で流す |
| 変更を試す環境 | `sqlmesh plan dev` | 別のスキーマを用意する(未計測) |
| 本番との表の差分 | `table_diff` | Enterprise プランのみ |

- SQLMesh の `plan` は、変更の分類と下流のモデルを表示します
- dbt の [`state:modified+`](https://docs.getdbt.com/reference/node-selection/methods) は、比較の元として本番の [manifest](https://docs.getdbt.com/reference/artifacts/manifest-json)(プロジェクトの全モデルの定義を書き出したファイル)を保存しておく必要があります
- SQLMesh の[ユニットテスト](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/tests.md)は `plan` の前に自動で流れ、失敗すると `plan` を止めました。dbt の [unit tests](https://docs.getdbt.com/docs/build/unit-tests) は `dbt test` か `dbt build` で流します
- `sqlmesh plan dev` は、変更したモデルだけを作り、上流は本番の表を参照します。dbt はプロファイルで別のスキーマを用意して作ります(今回は計測していません)
- SQLMesh の [`table_diff`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/tablediff.md) で、gold から外部との通信を除く変更を dev に入れて本番と比べると、本番にだけある行 294 行と、両方で一致した行 482 行を表示しました。dbt の差分の機能(compare changes)は [Enterprise プランのみ](https://docs.getdbt.com/docs/deploy/advanced-ci)で、自前の構成には無い機能です

## 状態 DB の障害とツールの更新

**SQLMesh は状態 DB に接続できないと、何もせずに止まりました。版の更新では、0.236.2 の状態 DB を 0.236.3 でそのまま使えました。**

この節は、本番と同じ Trino の環境で SQLMesh 0.236.3(本番で使っているイメージ)を使って試しました。

SQLMesh の状態 DB の接続先を、存在しないポートに向けて実行しました。SQLMesh は 5 秒で接続エラー(`connection refused`)になり、何もせずに終了しました(終了コード 1)。表は変わっていません。安全側に止まりますが、状態 DB が止まると変換はすべて止まります。dbt には状態 DB が無いので、この種の影響はありません。

ツールの更新では、0.236.2 で作った状態 DB を 0.236.3 でそのまま使えました。この 2 つの版の間には状態 DB の形式の変更(移行)がありません。移行が必要な版では、[公式のガイド](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/guides/migrations.md)によると SQLMesh が `sqlmesh migrate` の実行を求めて止まり、移行は 1 人が、誰も実行していないときに流す決まりです。

> Migration should be issued manually by a single user and the migration will affect all users of the project.

## 大量の再処理

**DuckDB では、どの方法も成功しました。本番の Trino では、区間をまとめて 1 クエリで処理する SQLMesh の既定がメモリ上限を超えたことがあり、`batch_size` で分割しました。**

6 時間分(197 万行)をまとめて処理し直しました。

| 方法 | 発行したクエリの数 | 結果 |
|---|---:|---|
| SQLMesh 既定(処理する区間をまとめて 1 クエリ) | 1 | 成功 |
| SQLMesh `batch_size 1`(1 区間ずつ、並行して処理) | 6 | 成功 |
| dbt microbatch(1 区間ずつ順に処理) | 6 | 成功 |
| dbt microbatch + [`concurrent_batches`](https://docs.getdbt.com/reference/resource-properties/concurrent_batches)(1 区間ずつ、並行して処理) | 6 | 成功 |

区間をまとめて 1 クエリで処理する既定は、1 回のクエリが扱う行数が多くなります。本番の Trino の 1 ノードあたりのメモリ上限は 1GB で、全期間を 1 クエリで作り直そうとしたときに超えました。

## Trino + Iceberg の環境でだけ起きたこと

**Iceberg の上で区間ごとの作り直しを並行に流すと、どちらのツールも書き込みが競合して失敗しました。ほかに、restatement の途中の失敗、dbt の `--full-refresh`、`table_diff`、ユニットテストで、DuckDB では起きない問題がありました。**

本番と同じ Trino 483 + Iceberg(カタログは Lakekeeper、保存先は SeaweedFS)で、SQLMesh 0.236.2 / dbt-core 1.12.5 / dbt-trino 1.10.6 を使ったときにだけ起き、DuckDB では起きなかったことです。

| 場面 | 起きたこと |
|---|---|
| 区間ごとの作り直しを並行に流す | どちらも `ICEBERG_COMMIT_ERROR` で失敗 |
| SQLMesh の restatement が途中で失敗 | 状態 DB は「処理済み」のまま |
| dbt の `--full-refresh` | 退避用の表の名前がぶつかって失敗 |
| SQLMesh の `table_diff` | `timestamp(6)` の列をキーにすると失敗 |
| SQLMesh のユニットテスト | 端末のタイムゾーンで結果が変わる |
| Iceberg の保守 | どちらもしない |

- **並行の作り直し**: SQLMesh(`batch_size 1`)も dbt(`concurrent_batches`)も、同じ Iceberg の表への削除どうしが競合して失敗しました。SQLMesh は並行数の上限 [`batch_concurrency`](https://github.com/SQLMesh/sqlmesh/blob/v0.236.3/docs/concepts/models/overview.md) を 1 にすると成功しました
- **restatement の途中の失敗**: 状態 DB に「処理済み」が残ったので、次の普通の実行は何もしませんでした。restatement をもう一度流す必要があります
- **`--full-refresh`**: dbt が古い表を退避する先の名前 `<モデル名>__dbt_backup` が、Lakekeeper の soft-delete(削除した表の名前を 7 日間残す設定)で残っていた名前とぶつかりました。gold は本体が消え、退避用の表と一時表だけが残りました。範囲を指定して流す方法は表の名前を変えないので、問題ありませんでした
- **`table_diff`**: `timestamp(6)`(マイクロ秒の精度の時刻)の列をキーにすると、`Requested timestamp precision '6' exceeds maximum supported precision: 3` で失敗しました
- **ユニットテスト**: タイムゾーン付きの時刻の列を使うと、日本時間の端末では区間の境界がずれて失敗し、`TZ=UTC` で通りました
- **Iceberg の保守**: どちらのツールも、小さなファイルをまとめる・古いスナップショットを消すことはしません。SQLMesh はロールバック用に前の版の物理表を残すので、silver で 4 版(各 197 万行)が残りました

## まとめ

- 失敗した区間と飛んだ区間は、SQLMesh が状態 DB の記録から次の実行で処理し直しました。dbt 2.0.5 は、失敗したバッチの後ろのバッチも実行せず、`dbt retry` でも処理し直せませんでした
- 式の変更は、SQLMesh が全期間を作り直し、dbt は処理した区間だけが新しい式になりました。ロールバックは SQLMesh が計算なし、dbt は全期間の計算し直しでした
- 上流の訂正は、どちらも普通の実行では拾わず、範囲指定の作り直しで直りました。SQLMesh は下流まで自動で、dbt は `+` で下流を選びます
- 入力の到着待ちは SQLMesh の signals で書けました。dbt の `source freshness` は検出だけで、実行は止めません
- SQLMesh は状態 DB が止まると全部止まります。Iceberg の上で区間ごとの作り直しを並行に流すと、どちらも失敗しました

## 参考

- 検証時の構成ファイル: [sqlmesh(本番のモデル)](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/sqlmesh)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public)。環境固有値はダミーに置換済み)。本記事の検証用のモデルとスクリプトは未公開です

- [dbt: About microbatch incremental models](https://docs.getdbt.com/docs/build/incremental-microbatch)
- [dbt: Configure incremental models(on_schema_change)](https://docs.getdbt.com/docs/build/incremental-models)
- [dbt: retry](https://docs.getdbt.com/reference/commands/retry)
- [dbt: Run results JSON file](https://docs.getdbt.com/reference/artifacts/run-results-json)
- [dbt: Manifest JSON file](https://docs.getdbt.com/reference/artifacts/manifest-json)
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
