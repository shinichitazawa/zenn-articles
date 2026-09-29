#!/usr/bin/env python3
"""文体チェック: ですます調で終わっていない文を articles/*.md から拾う。

CLAUDE.md の「ですます調を基本とする。だ・である調と混在禁止」を機械的に確かめる。
`python3 tools/check-taibun.py` を実行し、0 件になるまで直す。

## 判定の考え方

**違反語を列挙しない。** 列挙方式は必ず漏れる(「行う」「利く」「回せる」を 3 回続けて
落とした)。ですます形かどうかだけを見て、そうでなければ拾う。

  1. 活用語尾で終わる文  — 動詞の終止形はひらがなの「う段」、形容詞は「い」、断定は「だ」。
     箇条書きでもこれは違反。
  2. 地の文(箇条書き・表・見出しでない段落)で、ですます以外で終わる文 — 体言止めを含む。
     地の文の体言止めは、ですます調の中では だ・である として読める。

箇条書きの体言止めは既存記事の書き方に合わせて許容する(1 には当たらないので素通りする)。

## 対象外

front-matter / コードブロック / 表 / 見出し / 原文引用 / 画像キャプション / 脚注定義。
それ以外で文体の問題でないものは ALLOW に理由付きで挙げる。
"""
import pathlib, re, sys

FENCE = re.compile(r"^\s*(```|~~~)")
MASU = re.compile(r"(ます|ました|ません|ませんでした|ましょう|です|でした|でしょう|ください|下さい)$")
# 動詞の終止形(う段)・形容詞(い)・断定(だ)。ひらがなのみ。
KATSUYO = re.compile(r"[うくぐすずつぬぶむるいだ]$")
BULLET = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s)")

# 文体の問題ではないもの。内容の一部で照合する。
ALLOW = [
    # 条件・場合の列挙(「次の場合は〜」「〜に意味があるのは次の場合です」の下)
    "既に Crossplane に投資している", "等も管理したい",
    "文字起こしをクラスタ内で完結させたい", "最小要素は何か」を確かめたい",
    "呼び先を AWS 内に寄せたい",
    # 読者の問題意識の提示
    "ノードとして使いたい",
    # 課題・規則・仕様のラベル(周囲の項目と形を揃えている)
    "environment 毎に書き直す", "Kubernetes API で管理したくなる",
    "存在する場合だけ許可する", "列挙された遷移だけを許可する",
    # 鍵括弧で対比した語
    "のではなく「囲い込む」",
    # AI に渡す指示文そのもの。地の文ではないので命令形のまま
    "その他」を判定する", "そう判断した根拠を1文で返す",
    "確信が持てない語は `uncertain` に残す", "類似音の誤変換だけを直す",
    # 括弧内の注記。文末は括弧の外にある
    "残っていなければ呼ばれていない)", "**(必ず実行される)",
    # 箇条書きの太字ラベル(括弧を含むため fullmatch で落ちない)
    "永続ストレージの要求)に置く**", "**JetStream を耐久バッファとして挟む**",
    "必要な項目だけ埋める(請求書と見積書のみ支払期限を抽出",
    "**アロー関数が使えない**(Code ノード内",
    # 疑問の列挙
    "互換性が失われるのはどのような場合か",
    # ライセンス原文の要約(規範なので命令形のまま)
    "改善に使ってはならない(§V.3)",
    # 呼びかけ(「〜の確認を。」)
    "成立するかの確認を(「オープンウェイト",
    # 誤検出: 述語が markdown リンクのテキスト側にあり、括弧の剥がし方では到達できない
    "中身を復号できません](https://tailscale.com/kb/1232/derp-servers)",
    # 誤検出: 括弧の中に「。」があり、注記側が 1 文として切り出される
    "Agreement to terms of service is required` で拒否されます。実測)",
    "SIG Cloud Provider」と明記。2026-09 取得)",
]

OPEN, CLOSE = "（(「『【[", "）)」』】]"

def split_sentences(text):
    """「。」で文に割る。ただし **括弧の中の「。」では割らない**。

    出典の注記は括弧の中に「。」を持つことがある(「…より。2026-09 取得）」)。
    素朴に割ると「2026-09 取得）」が 1 文になり、体言止めとして誤検出する。
    """
    out, buf, depth = [], [], 0
    for ch in text:
        if ch in OPEN: depth += 1
        elif ch in CLOSE: depth = max(0, depth - 1)
        if ch == "。" and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail: out.append(re.sub(r"[:：]\s*$", "", tail))
    return [c for c in out if c.strip()]

def body_lines(path):
    lines = path.read_text().splitlines()
    out, in_fm, in_code, fence = [], False, False, None
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if i == 1 and s == "---": in_fm = True; continue
        if in_fm:
            if s == "---": in_fm = False
            continue
        m = FENCE.match(ln)
        if m:
            if not in_code: in_code, fence = True, m.group(1)
            elif s.startswith(fence): in_code, fence = False, None
            continue
        if in_code or not s: continue
        if s.startswith(("#", ">", "|", ":::")): continue
        if re.match(r"^!\[|^https?://\S+$|^\[\^[\w.-]+\]:", s): continue
        if re.match(r"^\*[^*].*\*$", s): continue   # 画像キャプション
        # 参考節のリンク行(丸ごと markdown リンク)。括弧の剥がし方で誤検出するため除く。
        if re.match(r"^\s*(?:[-*+]\s|\d+\.\s)?\[[^\]]+\]\(https?://[^)]+\)[^。]*$", s): continue
        out.append((i, ln))
    return out

# 文末に付きうる飾り: 脚注 [^x] / [1:8]、インラインコード、閉じ括弧、強調
# 閉じ括弧は含めない。含めると `…あります (注記)[^1]` の `)[^1]` をまとめて剥がしてしまい、
# 括弧書きの手前にある述語(あります)に到達できなくなる。
DECO = re.compile(r"(?:\[\^?[\w:.\-]+\]|`|[」』】]|\*\*|\*)+\s*$")
CLOSER = re.compile(r"[)\)\]】]+\s*$")
# 末尾の完結した括弧書き(注記)。述語はその手前にある。
PAREN = re.compile(r"\s*[((][^(()）]*[))]\s*$")

def _trim(s, drop_paren):
    prev = None
    s = re.sub(r"[。:：]+$", "", s.rstrip())
    while prev != s:
        prev = s
        s = DECO.sub("", s).rstrip()
        if drop_paren: s = PAREN.sub("", s).rstrip()
        else: s = CLOSER.sub("", s).rstrip()
        s = re.sub(r"[。:：]+$", "", s).rstrip()
    return s

def normalize(sent):
    """(判定に使う末尾, ですます判定に使う候補) を返す。

    末尾の括弧書きは注記のことも(「…です(実測)」)、述語そのもののことも
    (「…と明示します)」)ある。**どちらかがですます形なら違反にしない。**
    片方だけ見ると、括弧内に述語がある文を丸ごと誤検出する。
    """
    full = _trim(sent, drop_paren=False)
    bare = _trim(sent, drop_paren=True)
    return bare, (full, bare)

hits = []
for path in sorted(pathlib.Path("articles").glob("*.md")):
    for lineno, ln in body_lines(path):
        is_bullet = bool(BULLET.match(ln))
        text = BULLET.sub("", ln).rstrip()
        cands = split_sentences(text)
        for c in cands:
            c0 = c.strip()
            if re.fullmatch(r"\*\*[^*]+\*\*", c0): continue      # 見出し句
            if re.search(r"[((][^)())]*$", c0): continue         # 閉じていない括弧の中
            t, cands_masu = normalize(c)
            if not t or any(MASU.search(x) for x in cands_masu if x): continue
            if not re.search(r"[ぁ-んァ-ヶ一-龥]$", t): continue  # 日本語で終わる文だけ
            # 「:」で終わる行はコードブロック等の導入句。体言止めが自然なので活用語尾だけ見る。
            # ただし述語で終わる導入文(「…次の記述がある[^x]:」)は違反として拾う。
            intro = bool(re.search(r"[:：]\s*$", ln.rstrip()))
            why = "活用語尾" if KATSUYO.search(t) else (
                None if (is_bullet or intro) else "地の文の体言止め")
            if why is None: continue
            if any(a in ln for a in ALLOW): continue
            hits.append((path.name, lineno, why, c.strip()))

seen = set()
for f, l, why, s in hits:
    if (f, l, s) in seen: continue
    seen.add((f, l, s))
    print(f"{f}:{l}  [{why}]\n    …{s[-70:]}")
print(f"--- {len(seen)} 箇所 / {len({f for f,_,_,_ in hits})} 本 ---")
sys.exit(1 if seen else 0)
