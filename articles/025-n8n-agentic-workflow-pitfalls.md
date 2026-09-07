---
title: "n8n で AI エージェントのワークフローを組んで詰まった点 16 個"
emoji: "🕳️"
type: "tech"
topics: ["n8n", "bedrock", "ai", "workflow", "llm"]
published: false
---

## はじめに

n8n の AI Agent は、チャットモデル・メモリ・ツールをサブノードとして差すだけで組める手軽さがあります。ただ、業務で使える水準まで持っていこうとすると、チュートリアルには出てこない問題に次々ぶつかります。

本記事は、セルフホストの n8n(v2.33.3)で AI エージェントを使った業務ワークフローを8本作り(追記分も含めると計10本)、そのすべてを実際に実行して検証する過程で詰まった点の記録です。**動いた・動かなかったの実測が中心**で、原因が公式ドキュメントで裏付けられるものはその出典を、記載がないものは実測である旨を明示しています。

- 想定読者: n8n で AI エージェントを組んでいて、期待どおり動かない場面に当たっている方
- 検証環境: セルフホスト n8n 2.33.3 / モデルは Amazon Bedrock 経由
- 記事中の「実測」は、すべて筆者環境での実行結果です

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が公式ドキュメントを引用して検証しています。誤りや改善点があれば、コメント等でご指摘ください。
:::

## 1. 構造化出力が「空の応答」で落ちる

Structured Output Parser を付けたエージェントが、次のエラーで失敗しました(実測)。

```text
The AI model returned an empty response to the Structured Output Parser
```

n8n はエラー説明で原因の候補を示してくれます。

> This usually happens when the model runs out of tokens before it can generate the structured output. Try reducing the prompt length, increasing the model's max output tokens, or simplifying the output schema. To continue the execution when this happens, change the 'On Error' parameter in the root node's settings.
>
> — [N8nStructuredOutputParser.ts](https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/nodes-langchain/utils/output_parsers/N8nStructuredOutputParser.ts)(2026-08 時点で取得)

実際、**最大出力トークン数の指定漏れ**でした。AWS Bedrock Chat Model ノードのオプション `maxTokensToSample` は、upstream の実装で既定値が 2000 です。

```typescript
// 出典: https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/nodes-langchain/nodes/llms/LmChatAwsBedrock/LmChatAwsBedrock.node.ts
name: 'maxTokensToSample',
default: 2000,
description: 'The maximum number of tokens to generate in the completion',
```

日本語は英語よりトークン効率が悪いため、「要約 + 根拠の引用 + 判断理由」を JSON で返させると 2000 を超えます。**途中で切れた JSON はパースできず、パーサ側では「空の応答」として観測される**わけです。

対策は単純で、明示的に上げます。

```javascript
options: { temperature: 0.1, maxTokensToSample: 3000 }
```

UI では Options の「Maximum Number of Tokens」がこれにあたります。

![AWS Bedrock Chat Model ノードの Options で Maximum Number of Tokens を 3000 に設定した画面](/images/025-maxtokens-params.png)
*AWS Bedrock Chat Model ノードの Options。Maximum Number of Tokens を既定値から 3000 に引き上げている*

指定を忘れると既定値が使われるため、日本語で長めの構造化出力を求めた時点で失敗します。

## 2. 「配列で全部まとめて返して」は失敗しやすい

複数のリリースノートを評価させる処理で、最初はこう設計しました。

```json
{ "findings": [ { "project": "...", "summary": "...", "action": "..." } ], "overall": "..." }
```

1回の呼び出しで**配列を組み立てさせる**設計です。これは `Model output doesn't fit required format` で失敗しました(実測)。

同じ処理を、**1件ずつ・フラットなスキーマ**に変えたところ安定しました。

```json
{ "summary": "...", "affects_us": true, "evidence": "...", "action": "至急", "reason": "..." }
```

n8n の AI Agent ノードは入力アイテムごとに実行されるため、**N 件のアイテムを流せば自動的に N 回呼ばれます**。ループを自分で組む必要はありません。1件あたりの出力が小さくなり、トークン切れも起きにくくなります。

件数の絞り込み(prerelease の除外、バージョン比較など)は AI に任せず、コード側で確定させてから渡すと安定します。次の構成では、2つのリポジトリからリリースを取得して統合し、コードで対象を選別してからエージェントに1件ずつ渡しています。

![リリースを取得・統合し、コードで選別してからエージェントが1件ずつ判定するワークフロー](/images/025-a1-canvas.png)
*リリース判定ワークフロー。取得 → 統合 → Code ノードで選別 → エージェントが 1 件ずつ判定、の順に並ぶ*

## 3. ツールが失敗しても、エージェントはそのまま先へ進む

今回の検証で、いちばん気づきにくかった問題です。実行は成功として終わるため、結果を読まない限り異常だと分かりません。

サブワークフローをツールとして呼ぶエージェントを作り、実行したところ成功しました。しかし出力を見ると、**過去データがあるはずなのに「履歴が確認できない」と回答**しています。

![問い合わせをトリアージするエージェントに inquiry_history というサブワークフローツールを接続した構成](/images/025-a3-tool-canvas.png)
*トリアージ用エージェントに、サブワークフローをツール化した inquiry_history を接続した構成*

実行データを追うと、ツールの応答はこうでした(実測)。

```json
{ "error": "Workflow is not active and cannot be executed." }
```

エラー文字列がそのままエージェントに渡り、**エージェントはそれを「情報が得られなかった」と解釈して、正しく推論した**わけです。AI は間違っていません。壊れたツールを渡した側の問題です。

n8n のトレースには `tool_calls.completed: 1` と記録されます。**呼び出しは「完了」しています**。ツールが意味のある値を返したかどうかは別の話です。

対策として、ワークフローの成否だけでなく次を確認するようにしました。

- サブワークフローに実行履歴が残っているか(残っていなければ呼ばれていない)
- ツールノードの `executionStatus` が `error` になっていないか

エージェントの最終出力だけ見ていると、この種の障害は「AI の精度が低い」と誤診されます。

## 4. サブワークフローをツールにするには publish が要る

上記の原因は、**サブワークフローを publish していなかった**ことでした。

ツール側の設定はこうなっています。Source を Database にして呼び出し先の ID を指定し、引数は `$fromAI()` でエージェントに埋めさせます。

![Call n8n Workflow Tool の設定画面。Source は Database、Workflow は ID 指定、Workflow Inputs の email に $fromAI が入っている](/images/025-toolworkflow-params.png)
*Call n8n Workflow Tool の設定。Source=Database、Workflow は ID 指定、Workflow Inputs の email には `$fromAI` を指定している*

呼び出される側は Execute Workflow Trigger から始まる3ノードの小さなワークフローです。

![Execute Workflow Trigger から Data Table 検索を経て結果を要約して返すサブワークフロー](/images/025-subworkflow-tool.png)
*ツールとして呼ばれるサブワークフロー。Execute Workflow Trigger → Data Table 検索 → 結果の要約、の 3 段*

Call n8n Workflow Tool の[公式ドキュメント](https://docs.n8n.io/integrations/builtin/cluster-nodes/sub-nodes/n8n-nodes-langchain.toolworkflow/)には、**Database ソースで本番実行する場合はサブワークフローが publish されている必要があり、未 publish だと `Workflow is not active and cannot be executed` エラーになる**ことが明記されています(2026-09 時点。執筆当時は該当記載を見つけられず実測で確認しましたが、現行ドキュメントでは明文化されています)。実測の挙動(publish するまで前掲のエラー、publish 直後に成功)とも一致します。

publish が必要になる操作は他にもありました(すべて実測)。

| 操作 | publish 不要 | publish 必須 |
| --- | --- | --- |
| 手動実行・API からの実行 | ○ | |
| Schedule トリガーの自動発火 | | ● |
| 本番の Webhook / Form URL(`/webhook/...`) | | ● |
| サブワークフローとしての呼び出し | | ● |

手動実行だけで検証していると、この差に気づけません。**「テストでは動くのに本番で動かない」の典型的な原因**です。

## 5. Wait で止めた後、前のノードのデータは読めない

人間の承認を挟むために Wait ノードを使いました。承認後の処理で、前段のノードを参照したところエラーになりました(実測)。

```text
ExpressionError: Node '申請を整形' hasn't been executed
```

Wait ノードの[公式ドキュメント](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)には、待機中の挙動がこう説明されています。

> When the workflow pauses it offloads the execution data to the database. When the resume condition is met, the workflow reloads the data and the execution continues.

データベースへの退避と再読み込みが入るため、再開後の実行コンテキストは待機前と同一ではありません。`$('ノード名')` に依存した設計は壊れます。

そこで、**Wait を跨いでデータを運ばない**設計に変えました。

1. 待機に入る**前に**申請内容と AI の判定をデータベースへ保存する(`status: pending`)
2. 行のキーには `$execution.id` を使う
3. 再開後は前段を一切参照せず、`$execution.id` で**同じ行を更新**する(`status: decided`)

この形なら再開後に必要なのは実行 ID だけです。副次的に、承認待ちの一覧がそのままデータベースに残るという実務上の利点もありました。キャンバス上では「承認待ちとして保存 → 上長の承認を待つ → 決裁結果を反映」と、Wait の前後を保存と更新で挟む形になります。

![AI の規程チェック後、承認待ちとして保存してから Wait で待ち、再開後に決裁結果を同じ行へ反映するワークフロー](/images/025-a2-wait-canvas.png)
*承認待ちワークフロー。規程チェック → 承認待ちとして保存 → Wait ノードで停止 → 再開後に同じ行へ決裁結果を反映*

なお、再開用 URL は `$execution.resumeUrl` で参照できます。

> The Wait node provides the `$execution.resumeUrl` variable so that you can reference and send the yet-to-be-generated URL wherever needed, for example to a third-party service or in an email.

また `Limit Wait Time` を設定しておくと、承認が来ないまま放置された場合に自動で再開できます。承認フローでは**期限切れの扱いを決めておかないと、実行が無期限に滞留**します。設定画面では次のようになります(Resume を On Webhook Call、Limit Wait Time を3日、後述の Ignore Bots も有効)。

![Wait ノードの設定画面。Resume は On Webhook Call、$execution.resumeUrl の案内、Limit Wait Time 3 Days、Ignore Bots が有効](/images/025-wait-params.png)
*Wait ノードの設定。Resume=On Webhook Call、`$execution.resumeUrl` の案内表示、Limit Wait Time=3 Days、Ignore Bots=有効*

## 6. `ignoreBots` は curl も弾く

Wait の再開 Webhook に `ignoreBots: true` を付けたところ、`curl` からのアクセスが **HTTP 403** になりました(実測)。ブラウザの User-Agent に変えると 200 で通り、実行が再開しました。

```bash
# 実測の再現(URL は伏せています)
$ curl -s -o /dev/null -w '%{http_code}\n' "$RESUME_URL"
403   # 既定の curl UA は bot 判定
$ curl -s -o /dev/null -w '%{http_code}\n' -A "$BROWSER_UA" "$RESUME_URL"
200   # ブラウザ相当の UA なら再開される
$ curl -s -o /dev/null -w '%{http_code}\n' -A "$BROWSER_UA" "$RESUME_URL"
409   # 2 回目は二重承認として拒否
```

リンクプリフェッチによる誤承認を防ぐには有効な設定ですが、**動作確認を curl でやると原因不明の 403 に見えます**。テスト時は UA を指定するか、この設定を一時的に外して確認してください。

ちなみに、承認 URL に2回アクセスすると2回目は **HTTP 409** になりました。二重承認は n8n 側で防がれています。

## 7. Wait のフォームは `fieldName` が効かない

Wait を `resume: form` で使い、フォーム項目に `fieldName: 'decision'` を指定しました。しかし保存されたノード定義から `fieldName` が消えており、再開後の `$json.decision` は常に `null` でした(実測)。フォームの入力値は**ラベル名がキー**になります。

| 書いた指定 | 期待した参照 | 実際の挙動 |
|---|---|---|
| `fieldName: 'decision'` | `$json.decision` | 保存時に `fieldName` が消え、常に `null` |
| ラベル `承認しますか` のみ | — | `$json['承認しますか']` がキーになる |

Webhook 再開(`resume: webhook`)に切り替え、クエリパラメータで判定を受け取る方式にしたところ安定しました。承認リンクをメールやチャットで送る運用とも噛み合います。

## 8. Tailscale Ingress が承認者の身元を教えてくれる

これは問題ではなく、検証の副産物として分かったことです。

Tailscale の Ingress 経由で n8n を公開している環境で、Wait の再開 Webhook が受け取ったヘッダにこうした値が入っていました(実測)。

```text
tailscale-user-login: (承認者のメールアドレス)
tailscale-user-name:  (承認者の表示名)
tailscale-headers-info: https://tailscale.com/s/serve-headers
```

Tailscale は識別ヘッダの仕様を公開しており、`Tailscale-User-Login` などが該当します([Tailscale Serve のヘッダ](https://tailscale.com/s/serve-headers))。

承認 URL は「知っていれば誰でも呼び出せる」ため、本来は誰が承認したかを検証する必要があります。このヘッダを使えば、**追加の認証基盤なしに承認者を記録**できます。

ただし、そのまま認証として信頼するには条件があります。公式ドキュメントは次のように明記しています。

> When you use the identity headers to authenticate to a backend service, it's best practice to only have the service listen on localhost. Otherwise, any user that can call your service directly (rather than with the Serve URL) could trivially provide their own values for these HTTP headers. By listening only on localhost, this limits tampering to only other services running on the Serve device, and not anyone on your LAN or tailnet.
>
> — [Tailscale Serve のヘッダ](https://tailscale.com/s/serve-headers)

**ヘッダは経路を迂回すれば偽装できます**。Kubernetes 上で Service を立てている構成では、クラスタ内から直接 Pod を叩ける相手が同じヘッダを自由に付けられます。監査ログとして残す用途なら十分ですが、承認の可否そのものをこのヘッダだけで決めるなら、到達経路を Tailscale 側に限定できているかを先に確認してください。

## 9. AI に何を渡しているか、必ず自分の目で見る

これは仕組みの話ではなく、設計の姿勢の話です。

RSS フィードから記事を取得して AI に要約させるワークフローを作ったところ、出力が「未定」「不明」ばかりになりました。当初はモデルの能力不足を疑いましたが、**実際に渡っていた入力を確認したところ、本文がこれだけ**でした(実測)。

```html
<p>Release v1.21.0-pre.0</p>
```

Atom フィードは同じリリースについて複数のエントリを返すことがあり、片方は本文がほぼ空でした。本文がある方も HTML タグまみれで、文字数制限の大半を `data-hovercard-url` のような属性が占めていました。

**AI の回答は、渡された入力に対しては正しかった**ことになります。判断材料が無い以上、「不明」以外に答えようがありません。

同じ情報を GitHub API から取ると、本文は素の Markdown で 6千〜1万7千字あり、`⚠️ You may need to take action during the upgrade if you use ...` のような重要な記述もそのまま含まれていました。入力を替えただけで、出力は実用水準になりました。

出力が期待どおりでないときは、**まず入力を確認します**。基本的なことですが、n8n は各ノードの入出力を実行データで確認できるため、この手順を省く理由はありません。

## 10. マルチエージェントは「呼ばれない」ことがある

統括エージェントに2つの専門エージェント(調査担当・査読担当)をツールとして持たせ、「まず調査担当に調べさせ、次に査読担当に検証させよ」と指示しました。

実行後のトレースを見ると、**呼ばれていたのは査読担当だけ**でした(実測)。調査担当を経由せずに回答が生成されており、引用の出所が追えない状態になりました。

システムプロンプトで手順を書いても、**ツールを呼ぶかどうかはモデルの判断**です。順序や実行を保証したい工程は、ツールにせず通常のノードとして直列に並べる方が確実でした。

作り直した構成では、次のようにしています。

- 公式ドキュメントの取得: **HTTP Request ノード**(必ず実行される)
- 事実の抽出: 通常のエージェントノード
- 査読: 通常のエージェントノード。**判定の基準となる原文も一緒に渡す**
- 整形: 通常のエージェントノード

![公式ドキュメントを HTTP で取得し、調査担当・査読担当・編集担当のエージェントを直列に並べた構成](/images/025-a4v2-canvas.png)
*3 エージェント直列構成。HTTP Request で公式ドキュメントを取得したあと、調査担当 → 査読担当 → 編集担当の順にエージェントが並ぶ*

最初の版では査読担当に原文を渡していなかったため、「引用が原文にあるか」を判定できず、**正しい記述まで「原文に存在しない」と誤判定**していました。判断させるなら判断材料も渡す、という原則がここでも当てはまります。

## 11. SDK でワークフローを組むときの細かい注意点

n8n の Workflow SDK(ワークフローをコードで定義するための開発キット)でコードからワークフローを作る場合の実測メモです。

```text
# const merge = node({...}) と書いたときの検証エラー(実測)
'merge' is a reserved SDK function name
```

- **予約語と衝突する変数名が使えない**。`merge` のほか `node` / `trigger` など SDK の関数名は変数名にできません
- **すべてのノードに `output`(サンプル出力)が必要**。後続ノードの式検証に使われます
- **アロー関数が使えない**(Code ノード内の `jsCode` 文字列の中は可)
- ノードのバージョンとパラメータ名の対応を必ず確認する。型定義を確認せずに書いたところ、`typeVersion: 3` のノードに旧版(v2.2)のパラメータ名を書いてしまい、**UI で開いた瞬間に値が捨てられる**という壊れ方をしました

最後の点は特に厄介です。作成 API は通り、一見すると成功しているように見えます。ノードを追加する前に必ず型定義を確認してください。

## 追記: その後の検証で直面した問題(2026-08-14)

初稿のあと、脆弱性トリアージと「会議音声 → ToDo 抽出」の 2 本を追加で作りました。そこで新たに直面したものを追記します。いずれも筆者環境での実測です。

| # | 内容 | 種別 |
|---|---|---|
| 12 | HTTP Request は応答ヘッダを 5 分しか待てない | n8n の実装制約 |
| 13 | Code ノードの日付計算は UTC でずれる | 実行環境 |
| 14 | `executeOnce` は入力を先頭 1 件に切り詰める | 仕様の読み違え |
| 15 | `ignoreBots` は「Mozilla/5.0」だけの UA も弾く | 6 の追補 |
| 16 | モデルのコンテンツフィルタで出力が空になる | モデル側 |

## 12. HTTP Request は応答ヘッダを 5 分しか待てない

クラスタ内に置いた文字起こしサービス(Whisper)を HTTP Request ノードで呼んだところ、ノードの timeout を 30 分に設定したにもかかわらず、**約 313 秒で `socket hang up` になりました**(実測 2 回、いずれも約 5 分 15 秒)。

原因は n8n ではなく、Node.js の HTTP クライアント(undici)の既定値です。undici の [Client オプション](https://github.com/nodejs/undici/blob/main/docs/docs/api/Client.md)にはこうあります。

> `headersTimeout` {number|null} The timeout, in milliseconds, the parser waits to receive the complete HTTP headers before the request times out. Use `0` to disable it entirely. **Default:** `300e3`.

**応答ヘッダが 300 秒以内に届かないと切断される**、という制限です。文字起こしのような「処理が終わるまで一切応答しない」同期 API は、処理が 5 分を超えた時点で必ずこれに当たります。ノードの timeout 設定はこのヘッダ待ちには効きませんでした(実測)。

対処は呼び出しの非同期化です。呼び先に「受付だけして即応答し、結果は別エンドポイントで返す」薄いラッパーを足し、n8n 側は次の形にしました。

```text
投入(POST /jobs → 即座に job_id が返る)
  → Wait(30秒)
  → 結果を取得(GET /jobs/{id}、即応答)
  → IF status = done → 後続へ
       まだなら → Wait に戻る(ループ)
```

各リクエストが秒で返るため、ヘッダ待ちの制限に当たりません。処理時間が読めない外部サービスを呼ぶ場合は、最初からこの形にしておくと安全です。あわせてワークフロー設定の実行タイムアウト(Workflow Settings → Timeout)を設定し、ジョブが返らない場合の無限ループを止めています。

## 13. Code ノードの日付計算は UTC でずれる

会議の「あす」を絶対日付に変換する処理で、**「あす」が当日になる**ずれが出ました(実測)。

原因は、Code ノードがタスクランナー(n8n 本体とは別プロセスでユーザーコードを実行する仕組み。[公式: Task runners](https://docs.n8n.io/hosting/configuration/task-runners/))で実行され、その環境のタイムゾーンが UTC だったことです。`new Date()` のローカル getter(`getDate()` など)で日付を組み立てると、JST との 9 時間差で日付境界がずれます。コンテナに `GENERIC_TIMEZONE=Asia/Tokyo` を設定していても、この getter には効きませんでした(実測)。

対処は、日付演算を UTC getter + 明示オフセットに統一することです。

```javascript
// ✗ ローカル getter(ランナーが UTC だと 9 時間ずれる)
const d = new Date(held + 'T00:00:00+09:00');
d.setDate(d.getDate() + 1);           // 「あす」のつもりが当日になった

// ○ UTC 基準で計算し、日付の加算はミリ秒で行う
const base = new Date(held + 'T00:00:00Z');
const next = new Date(base.getTime() + 86400000);
const y = next.getUTCFullYear();      // getter も UTC 側を使う
```

`$now` は n8n が Luxon で返すため `$now.setZone('Asia/Tokyo')` で明示すれば安全です。素の `Date` を使う箇所にだけ注意が要ります。

## 14. `executeOnce` は入力を先頭 1 件に切り詰める

未完了 ToDo を全件読んで分類する Code ノードに `executeOnce: true` を付けたところ、**2 件あるはずの集計が 1 件になりました**(実測)。

```text
# 実測時の入出力
入力            : 2 items(ToDo A, ToDo B)
executeOnce なし : $input.all() → [ToDo A, ToDo B](期待どおり)
executeOnce あり : $input.all() → [ToDo A]      (先頭 1 件に切り詰め)
```

これは仕様どおりの動作です。[公式ドキュメント](https://docs.n8n.io/build/understand-workflows/workflow-components/work-with-nodes)は Execute Once をこう説明しています。

> The node executes once, with data from the first item it receives. It doesn't process any extra items.

「1 回だけ実行する」ではなく「**先頭 1 件のデータで** 1 回だけ実行する」です。`$input.all()` で全件を集計するつもりの Code ノードに付けると、`$input.all()` 自体が 1 件しか返しません。

使い分けはこうなります。

- **付ける**: 複数アイテムが流れてきても 1 回で済む副作用ノード(サマリ通知の送信など)。ただし参照は `$('ノード名').all()` で行う
- **付けない**: `$input.all()` で全アイテムを読む集計 Code ノード。`runOnceForAllItems` モード自体が「全アイテムで 1 回」なので、そもそも重ねる必要がありません

## 15. `ignoreBots` は「Mozilla/5.0」だけの UA も弾く

6 節の追補です。`ignoreBots: true` の Webhook を `curl -A 'Mozilla/5.0'` で叩いたところ、それでも拒否されました(実測)。完全なブラウザ UA 文字列(`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...`)にすると通りました。

```bash
$ curl -s -A 'Mozilla/5.0' "$WEBHOOK_URL"
{"code":403,"message":"Authorization data is wrong!"}
# 認証エラーのような文言だが、実体は bot 判定
```

実ブラウザからの操作には影響しませんが、curl やスクリプトで動作確認する場合は UA を完全な形で渡す必要があります。エラー文言から原因にたどり着きにくいので、`ignoreBots` 付き Webhook が 403 を返したらまず UA を疑ってください。

なお、この紛らわしいエラー文言は upstream へ報告済みです([n8n-io/n8n#36363](https://github.com/n8n-io/n8n/issues/36363)。Webhook ノードはコミュニティ PR を受け付けない方針のため、修正ブランチを添えた issue の形で提出しています)。

## 16. モデルのコンテンツフィルタで出力が空になる

脆弱性アドバイザリを読ませて自環境への影響を判定させるワークフローで、Bedrock の Amazon Nova が**構造化出力をまったく返さない**事象が出ました。モデルの生出力を確認すると、次の 1 行だけが返っていました(実測)。

```text
- The generated text has been blocked by our content filters.
```

脆弱性・悪用といった話題がフィルタに触れたものです。パーサ側では「形式に合わない出力」としてしか見えないため、原因の切り分けにはモデルの生出力(実行データの ai_languageModel 側)を見る必要があります。

対処は 2 つ併用しました。

1. プロンプトの言い換え。「悪用条件」「攻撃手法」ではなく「アドバイザリが対象としている条件」「必要な保守作業」という保守業務の語彙に寄せると通りました(実測。20 件中 19 件が成功)
2. それでも個別に落ちる項目はあるため、AI ノードを `onError: continueRegularOutput` にして、失敗した項目はフラグ付きの「要確認」として後続に流し、1 件の失敗で全体を止めない

セキュリティ関連の文書を扱うワークフローでは、この種のフィルタ起因の失敗を設計に織り込んでおく必要があります。

## まとめ

1. **`maxTokensToSample` の既定は 2000**。日本語の構造化出力はすぐ超えるため明示指定が要る
2. **1回の呼び出しで配列を作らせない**。1件ずつ・フラットなスキーマにすると安定する
3. **ツールが失敗してもエージェントは先へ進む**。エラー文字列は「情報なし」として解釈される
4. **サブワークフローをツールにするには publish が必要**(実測どおりで、現行の公式ドキュメントにも明記あり)
5. **Wait を跨いで前段データは読めない**。待機前に保存し、`$execution.id` で更新する設計にする
6. **出力を疑う前に入力を見る**。AI の回答は、たいてい渡した入力に対しては正しい
7. **応答ヘッダの待ち時間は 300 秒が上限**(undici の既定)。長い処理は投入 + ポーリングに分ける
8. **Code ノードの `Date` は UTC 前提で書く**。`executeOnce` は集計ノードに付けない
9. **コンテンツフィルタでも出力は空になる**。AI ノードは 1 件の失敗で全体を止めない設定にしておく

エージェントの精度を上げる作業の大半は、プロンプトの工夫ではなく、**渡すデータと配線の設計**でした。

## 参考

- [Wait node — n8n Docs](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)
- [Call n8n Workflow Tool — n8n Docs](https://docs.n8n.io/integrations/builtin/cluster-nodes/sub-nodes/n8n-nodes-langchain.toolworkflow/)
- [AI Agent node — n8n Docs](https://docs.n8n.io/integrations/builtin/cluster-nodes/root-nodes/n8n-nodes-langchain.agent/)
- [AWS Bedrock Chat Model node — n8n Docs](https://docs.n8n.io/integrations/builtin/cluster-nodes/sub-nodes/n8n-nodes-langchain.lmchatawsbedrock/)
- [n8n upstream: LmChatAwsBedrock.node.ts](https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/nodes-langchain/nodes/llms/LmChatAwsBedrock/LmChatAwsBedrock.node.ts)
- [Tailscale Serve が付与するヘッダ](https://tailscale.com/s/serve-headers)
- [undici Client オプション(headersTimeout)](https://github.com/nodejs/undici/blob/main/docs/docs/api/Client.md)
- [Work with nodes(Execute Once)— n8n Docs](https://docs.n8n.io/build/understand-workflows/workflow-components/work-with-nodes)
- 検証時の構成ファイル: [n8n](https://github.com/shinichitazawa/k8s-deploy-public/tree/main/n8n)([k8s-deploy-public](https://github.com/shinichitazawa/k8s-deploy-public) commit [`4df788b`](https://github.com/shinichitazawa/k8s-deploy-public/commit/4df788b) 時点。環境固有値はダミーに置換済み)
