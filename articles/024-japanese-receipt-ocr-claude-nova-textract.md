---
title: "日本語の領収書を読ませるなら Claude / Nova / Textract のどれか"
emoji: "🧾"
type: "tech"
topics: ["bedrock", "textract", "claude", "ocr", "n8n"]
published: false
---

## はじめに

領収書や請求書を自動で読み取って経費精算に流したい、という要件はよくあります。本記事で比較したのは次の3つです。

1. **Amazon Textract**(帳票専用の OCR。`AnalyzeExpense` は領収書・請求書に特化)
2. **Amazon Nova Lite**(Bedrock のマルチモーダルモデル。低コスト)
3. **Claude Haiku 4.5**(Bedrock 経由のマルチモーダルモデル)

画像を読めるモデルは他にも多数あります。この3つに絞ったのは、次の制約が先にあったためです。

- 実行基盤が AWS で、セルフホストの n8n から**キーを持たずに呼べる**こと(OIDC 経由で IAM ロールを引き受ける構成のため、呼び先を AWS 内に寄せたい)
- 経理帳票を扱うため、**どのリージョンで処理されるかを説明できる**こと(後述の `jp.` 推論プロファイル)
- 自前でモデルをホストせず、**運用対象を増やさない**こと

したがって、他社のマルチモーダルモデル、OSS の VLM、Google Document AI や Azure AI Document Intelligence といった選択肢は、今回は比較対象に入れていません。**「この3つが最良」という主張ではなく、上記の制約下で選べた3つを実測した記録**です。

そのうえで、当初は「帳票なら専用サービスの方が確実だろう」と判断していました。しかし**同一の日本語領収書を3つに読ませたところ、結果は逆**でした。

本記事は、その実測結果と、なぜそうなるのかを公式ドキュメントで裏付けた記録です。

- 想定読者: AWS で帳票 OCR の実装方式を選定している方
- 検証日: 2026-08(モデル・サービスの仕様は変わり得ます)
- 検証環境: セルフホスト n8n から Bedrock / Textract を呼び出し

:::message
本記事の文章生成・編集には AI (Anthropic Claude) を活用しています。技術的事実については、筆者が公式ドキュメントを引用して検証しています。誤りや改善点があれば、コメント等でご指摘ください。
:::

## 検証に使った領収書

日本の領収書によくある要素を入れた画像を用意しました。これを3つすべてに同じ条件で読ませています。

![検証に使った合成領収書。表題「領収書」、宛名「株式会社◯◯ 御中」、金額 ¥18,700、10% と 8% の複数税率、適格請求書発行事業者の登録番号、発行元の店名・住所・電話番号が印字されている](/images/024-receipt-sample.png)

含めた要素は次のとおりです。

- 表題「領収書」、宛名「株式会社◯◯ 御中」
- 合計 ¥18,700
- **軽減税率を含む複数税率**(10%対象 ¥12,000 / 消費税 ¥1,200、8%対象 ¥5,093 / 消費税 ¥407)
- **適格請求書発行事業者の登録番号**(T + 13桁)
- 但し書き「会議用飲食代として」
- 発行元の店名・住所・電話番号

正解データが分かっている合成画像です。実店舗のレシートではなく、印字が明瞭な条件で比較しています。**つまり実務より易しい条件**であることに注意してください。手書き、感熱紙の退色、折れ、斜めからの撮影といった条件では結果が変わり得ます。

なお、掲載した画像の**社名・住所・登録番号は、記事用のダミー値(◯◯・△△など)に差し替えています**。実測時はもう少し実在の帳票に近い文字列を使いましたが、実在の企業・店舗と紛らわしいため置き換えました。以降に載せる出力例も、同じダミー値に揃えています。読み取り精度の比較結果は、これらの文字列の中身には依存しません。

## 結果

| 項目 | Textract | Nova Lite | Claude Haiku 4.5 |
| --- | --- | --- | --- |
| 発行元(店名) | 抽出なし | 架空の社名を出力 | 正確 |
| 宛名との区別 | 抽出なし | できていない | 正確 |
| 発行日 | 抽出なし | 正確 | 正確 |
| 合計金額 | 抽出なし | 正確 | 正確 |
| 複数税率の合算 | 断片のみ | 誤り | 正確 |
| 登録番号(T+13桁) | 抽出なし | 伝票番号と混同 | 正確 |
| 電話番号 | `VENDOR_PHONE` として正確 | — | — |
| 明細 | 空 | — | 抽出 |
| 帳票種別の判定 | 機能なし | — | 「領収書」と判定 |

以下、それぞれの詳細です。

### Textract: 型付けできたのは電話番号だけ

`AnalyzeExpense` の応答から、値が入った項目を並べるとこうなりました(実測)。

```text
VENDOR_PHONE         [TEL      ] = '03-1234-5678'   conf=100
OTHER                [No.      ] = '2026-0810-0473' conf=100
OTHER                [(10%)    ] = '¥12,000'        conf=100
OTHER                [(10%)    ] = '¥1,200'         conf=100
OTHER                [(8%)     ] = '¥407'           conf=100
LineItemGroups: (空)
```

`TOTAL` `VENDOR_NAME` `INVOICE_RECEIPT_DATE` といった中核フィールドは**1つも埋まりませんでした**。`AnalyzeExpense` が返す標準フィールドは公式に列挙されており、上記はいずれもその一覧に含まれています([Analyzing Invoices and Receipts](https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html))。同ページには、当てはまらなかった項目の扱いも明記されています。

> Fields that do not align with the standard taxonomy are categorized as `OTHER`.
>
> — [Analyzing Invoices and Receipts](https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html)

つまり金額は文字としては読めているものの、**標準の意味づけに1つも当てはまらず `OTHER` に落ちている**状態です。信頼度は 100 ですが、これは「文字が読めた」ことへの信頼度であって、項目の同定が正しいことを意味しません。

これは不具合ではなく、**仕様どおり**です。Amazon Textract の FAQ には対応言語がこう書かれています。

> Amazon Textract can extract printed text, forms and tables in English, German, French, Spanish, Italian and Portuguese. Amazon Textract also extracts explicitly labeled data, implied data, and line items from an itemized list of goods or services from almost any invoice or receipt **in English** without any templates or configuration.
>
> — [Amazon Textract FAQs](https://aws.amazon.com/textract/faqs/)

対応言語に**日本語は含まれていません**。とくに請求書・領収書からの項目抽出は "in English" と明記されています。日本語帳票で中核フィールドが埋まらないのは、想定された挙動です。

公式ページには「Amazon Textract also identifies vendor names that are critical for your workflows but may not be explicitly labeled.」ともあり、ロゴ内の店名すら拾う想定です([同ページ](https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html))。それが日本語では機能しなかった、という結果になります。明細行(`LineItemGroups`)も空でした。

### Nova Lite: 存在しない社名を出力した

Nova Lite はマルチモーダルモデルです。[Amazon Nova のモデル仕様](https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html)によると、入力モダリティは次のとおりです。

| | Nova Pro | Nova Lite | Nova Micro |
| --- | --- | --- | --- |
| Input modalities | Text, Image, Video | Text, Image, Video | Text |
| Context Window | 300k | 300k | 128k |

出典: [What is Amazon Nova?](https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html)(2026-08 時点で取得。Amazon Nova V1 のユーザーガイドです。後継の [Amazon Nova 2](https://docs.aws.amazon.com/nova/latest/nova2-userguide/whats-new.html) は別ガイドで、本記事では検証していません)

**Nova Micro はテキスト専用**なので画像は扱えません。画像を読ませるなら Lite 以上が必要です。Nova Lite は公式に「low-cost multimodal model that processes text, images, and video inputs for tasks like **document analysis** and visual Q&A」と位置づけられています([Nova Lite モデルカード](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html))。

しかし実測では、発行元として**画像のどこにも存在しない社名**を出力しました。宛名(株式会社◯◯ 御中)でもありません。

これは「一発で読み取りと解釈を同時にやらせたせいでは」と考え、工程を2段に分けて再検証しました。

![Nova Lite に逐語転記させ、その転記テキストから Nova Micro が項目を抽出し、コードが両者を照合する2段構成のワークフロー](/images/024-a5v2-canvas.png)

1. Nova Lite に**逐語転記だけ**させる(「解釈・要約・補完を一切しない」「画像に無い語を絶対に書かない」と明示)
2. 転記テキストから項目を抽出する

結果、**転記の段階で捏造が起きました**(実測)。

```text
株式会社 ■■■■               ← 画像に存在しない社名(伏せ字は筆者による)
東京都新宿区西新宿x-x-x        ← 画像に印字された区名とは別の区
商品名: 株式会社 ■■■■ の 商品代金   ← 画像に無い行
代表者: T1234567890123        ← 登録番号を「代表者」と誤ラベル
```

正しく読めたのは日付・合計・税額(1,200 / 407)だけで、**店名も住所も差し替わっていました**。「書いてある文字をそのまま写す」という最も制約の強い指示ですら守られていないため、プロンプトの改良で解決できる種類の問題ではないと判断しました。

:::message alert
これは Nova Lite が一般に低品質という意味ではありません。**日本語の帳票画像から固有名詞を正確に読む**という特定タスクで、要求水準に届かなかったという実測結果です。
:::

### Claude Haiku 4.5: 全項目正解

同じ画像を Claude Haiku 4.5 に読ませた結果です(実測)。

```json
{
  "doc_type": "領収書",
  "confidence": "高",
  "issuer": "居酒屋 △△亭 ××店",
  "counterparty": "株式会社◯◯",
  "issued_on": "2026-08-08",
  "total": 18700,
  "tax_total": 1607,
  "invoice_no": "T1234567890123",
  "account_item": "会議費"
}
```

**全項目正解、捏造なし**でした。とくに次の3点は他の2つができなかったことです。

- **発行元と宛名の区別**: 「御中」が付くのが宛先である、という日本の商習慣を踏まえた判定
- **複数税率の合算**: 1,200 + 407 = 1,607 を自分で計算
- **登録番号と伝票番号の区別**: `T1234567890123` と `No. 2026-0810-0473` を取り違えない

Bedrock で利用できる Claude のうち、画像入力に対応しているかは `list-foundation-models` で確認できます(実測)。

```bash
aws bedrock list-foundation-models --region ap-northeast-1 \
  --query 'modelSummaries[?contains(modelId,`haiku-4-5`)].{id:modelId,in:inputModalities}'
```

```json
[{ "id": "anthropic.claude-haiku-4-5-20251001-v1:0", "in": ["TEXT", "IMAGE"] }]
```

## 帳票種別の判定まで一度に行えたのは LLM だけだった

見落としやすい差です。Textract の `AnalyzeExpense` は「これは領収書である」という前提で項目を抜く API であり、**種別を判定する機能はありません**。領収書・請求書・見積書・納品書が混在して届く運用では、種別ごとに API や後処理を分ける設計が必要になります。

LLM なら同じ1回の呼び出しで種別判定まで含められます。実際に組んだのが次の構成です。読み取りと種別判定を1つのエージェントで行い、コードで検証したうえで、判定結果に応じて経費精算・支払管理・案件管理・要人手確認へ振り分けています。

![Claude が帳票種別を判定して読み取り、コードで検証したのち種別ごとに振り分けるワークフロー](/images/024-a6-canvas.png)

指示は次のような内容です。

- 表題や記載内容から「領収書 / 請求書 / 見積書 / 納品書 / その他」を判定する
- 判定の確信度(高・中・低)と、そう判断した根拠を1文で返す
- 種別に応じて必要な項目だけ埋める(請求書と見積書のみ支払期限を抽出、など)

入口を1つにして、判定結果で経費精算・支払管理・案件管理に振り分ける、という構成が組めます。

## 実運用で必要になった2つの追加要件

### データの所在を国内に留める

経理帳票は社外に出したくない、というより「どこで処理されるか」を説明できる必要があります。Bedrock の推論プロファイルには地理を限定するものがあり、`ap-northeast-1` では次のように `jp.` 系が利用できました(実測)。

```text
jp.anthropic.claude-haiku-4-5-20251001-v1:0   JP Anthropic Claude Haiku 4.5   ACTIVE
jp.anthropic.claude-sonnet-4-6                JP Anthropic Claude Sonnet 4.6  ACTIVE
```

クロスリージョン推論プロファイルの考え方は公式に説明があります。地理単位のプロファイルを選べば、その地理内のリージョンで処理されます。

> When you choose an inference profile tied to a specific geography, Amazon Bedrock automatically selects a commercial AWS Region within that geography to process your inference request.
>
> — [Route model inference requests across AWS Regions with cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)

同ページによると、データ所在の要件がある場合は Global ではなく Geographic を選ぶことが推奨されています。

### Anthropic モデルは初回にユースケース提出が要る

Bedrock で Anthropic のモデルを初めて使うアカウントでは、モデルを呼ぶ前に一度だけフォームの提出が必要です。

> **Note:** For Anthropic models, if you're a first-time customer, then you must complete the First Time Use (FTU) form before you can invoke a model. To submit your use case details, use the Amazon Bedrock console or the `PutUseCaseForModelAccess` API operation.
>
> — [How do I resolve "Access denied" errors when I try to invoke serverless foundation models in Amazon Bedrock?](https://repost.aws/knowledge-center/bedrock-serverless-models-access-denied)

未提出の状態で呼ぶと、次のエラーになります(実測)。

```text
Model use case details have not been submitted for this account.
Fill out the Anthropic use case details form before using the model.
```

API で提出する場合、送るのは base64 エンコードした JSON です。項目は公式リファレンスに例示されています。

```python
# 出典: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_PutUseCaseForModelAccess.html
form_data = {
    "companyName": COMPANY_NAME,
    "companyWebsite": COMPANY_WEBSITE,
    "intendedUsers": INTENDED_USERS,
    "industryOption": INDUSTRY_OPTION,
    "otherIndustryOption": OTHER_INDUSTRY_OPTION,
    "useCases": USE_CASES
}
```

なお、モデルアクセスの管理手順(オファーの一覧 → FTU 提出 → 規約締結 → 利用可否確認)は [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) に整理されています。締結状況は次で確認できます(実測)。

```bash
aws bedrock get-foundation-model-availability --region ap-northeast-1 \
  --model-id anthropic.claude-haiku-4-5-20251001-v1:0
```

```json
{
  "agreementAvailability": { "status": "NOT_AVAILABLE" },
  "authorizationStatus": "AUTHORIZED",
  "entitlementAvailability": "AVAILABLE",
  "regionAvailability": "AVAILABLE"
}
```

IAM もリージョンも問題ないのに呼べない場合、`agreementAvailability` を見ると切り分けが早いです。

## 精度に頼り切らない設計

読み取り精度が上がっても、抽出された値をそのまま台帳に登録するのは避けたほうがよいと考えています。今回の検証でも Nova Lite が架空の社名を出力しました。そこで、抽出結果をコード側で検証する層を挟みました。

```javascript
// 抽出した発行元が、読み取り全文に実在するかを照合する
const norm = (s) => String(s || '').replace(/[\s　,¥￥-]/g, '');
if (vendor && norm(rawText).indexOf(norm(vendor)) === -1) {
  flags.push(`発行元「${vendor}」が読み取り全文に存在しない(捏造の疑い)`);
}

// 税額と本体価格の整合を検算する
const net = total - taxTotal;
const rate = net > 0 ? taxTotal / net : 0;
if (rate < 0.07 || rate > 0.11) {
  flags.push(`税率が想定外(本体 ${net} 円 / 税 ${taxTotal} 円 = ${(rate * 100).toFixed(1)}%)`);
}
```

この2つだけでも、実測では次の効果がありました。

- Nova Lite が税額内訳を読み違えた際、**検算が不整合を検知**して人の確認フラグが立った
- ただし**捏造された社名は、読み取り全文そのものが捏造されていたため、照合では検出できなかった**

後者は重要です。「モデルの出力を別のモデルの出力と突き合わせる」方式では、上流の読み取り自体が誤っている場合に検出できません。**読み取り精度そのものが一定水準に達していることが前提**になります。

## まとめ

1. **日本語の領収書では Textract より Claude の方が精度が高い**という実測結果でした。「帳票は専用 OCR」という一般論は日本語では成り立ちません
2. その理由は公式に明記されています。Textract の対応言語は英語・独語・仏語・西語・伊語・葡語で、**請求書・領収書の項目抽出は英語のみ**([FAQ](https://aws.amazon.com/textract/faqs/))
3. **Nova Micro はテキスト専用**で画像を扱えません。Nova Lite はマルチモーダルですが、日本語帳票の固有名詞で捏造が発生しました(実測)
4. **種別判定まで1回の処理で行えたのは LLM だけ**でした。Textract の `AnalyzeExpense` に種別を判定する機能はありません
5. 精度が上がっても検証層は必要です。ただし**上流の読み取りが誤っていると照合では検出できない**ため、まずモデル選定で精度を確保することが先になります

コスト面は本記事では比較していません。Textract は枚数課金、Bedrock はトークン課金で単純比較が難しく、実運用の枚数・画像サイズで見積もる必要があります。

## 参考

- [Amazon Textract FAQs](https://aws.amazon.com/textract/faqs/)
- [Analyzing Invoices and Receipts — Amazon Textract 開発者ガイド](https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html)
- [What is Amazon Nova?(Amazon Nova V1 ユーザーガイド)](https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html)
- [Nova Lite モデルカード](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html)
- [Route model inference requests across AWS Regions with cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
- [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
- [PutUseCaseForModelAccess](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_PutUseCaseForModelAccess.html)
- [Bedrock でモデル呼び出しが Access denied になる場合 — AWS re:Post](https://repost.aws/knowledge-center/bedrock-serverless-models-access-denied)
