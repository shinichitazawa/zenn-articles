# devto — 英語版(dev.to へのクロスポスト)

`articles/` の記事を英訳し、dev.to(Forem)へ投稿するための Markdown を置く。

## Zenn 版との違い

| 項目 | Zenn | dev.to |
|---|---|---|
| front matter | `title` / `emoji` / `type` / `topics` / `published` | `title` / `published` / `description` / `tags`(最大 4)/ `canonical_url` / `cover_image` |
| 注記 | `:::message` … `:::` | `{% details タイトル %}` … `{% enddetails %}` |
| 図 | mermaid が描画される | **mermaid は描画されない**(エディタガイドに記載なし)。`text` のコードブロックか画像に置き換える |
| 画像 | `/images/foo.png`(リポジトリ内) | 絶対 URL が必要。エディタでアップロードするか raw URL を使う |

`canonical_url` には Zenn の URL を入れる。転載であることを検索エンジンに示し、評価が分散するのを避けるため
([Forem API のフィールド定義](https://developers.forem.com/api/v1))。

## 投稿

API キーは dev.to の Settings → Extensions で発行し、**git には置かない**。
投稿は `POST https://dev.to/api/articles`(ヘッダ `api-key`)。

```bash
# キーの投入(値はチャットに出さない。対話実行のみ)
! bash /home/st/setup-devto-key.sh

# 下書きとして投稿(published: false のまま送る → dev.to のダッシュボードで確認して公開)
bash /home/st/devto-publish.sh devto/031-cluster-autoscaler-sakuracloud-provider.md
```

## 方針

- 翻訳は逐語訳にしない。日本の読者前提の文脈(さくらのクラウドが何か、別記事が日本語であること)は
  1 行で補う。
- 出典 URL は Zenn 版と同一にする。日本語のみの一次資料には `(Japanese)` と付す。
- 実測値は Zenn 版と同じ数字を使う。片方だけ更新しない。
