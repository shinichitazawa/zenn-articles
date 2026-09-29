#!/usr/bin/env python3
"""文体チェック: だ・である調の文末を articles/*.md から拾う。

CLAUDE.md の「ですます調を基本とする。だ・である調と混在禁止」を機械的に確かめる。
`python3 tools/check-taibun.py` で実行し、1 件でも出たら直す。

対象外(既存記事の書き方に合わせている):
  - front-matter / コードブロック / 表 / 見出し / 原文引用 / 画像キャプション
  - 全体が **…** で囲まれた見出し句(箇条書きの太字ラベル)
  - 括弧の中だけで終わる注記
  - AI に渡す指示文のリスト(検出には出るが直さない。周囲の文体で判断すること)

見落としがちな形(過去に実際に漏らした):
  - 脚注付き「…がある[^x]:」  ← 「。」で終わらないので素朴な検索では拾えない
  - 「…でした。」を誤って違反と数える  ← 「した」に負の後読みが要る
  - 「行う」「使う」のようなう段の動詞終止形  ← 「する」だけ見ても足りない
"""
import re, sys, pathlib

FENCE = re.compile(r"^\s*(```|~~~)")

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
        if re.match(r"^\*[^*].*\*$", s): continue   # 画像キャプション(既存記事も体言止め)
        out.append((i, ln))
    return out

# 文末に付きうる飾り: 脚注 [^x] / [1:8]、インラインコード `、閉じ括弧、強調
DECO = re.compile(r"(?:\[\^?[\w:.\-]+\]|`|[)\)」』\]】]|\*\*|\*)+\s*$")

DEARU = re.compile(
    r"(?:"
    r"である|であった|であり|ではない|ではなかった|でない|だった|だろう|"
    r"ている|ていた|ていない|ておく|てある|てしまう|てしまった|"
    r"される|された|されない|されている|されていた|"
    r"する|(?<![まで])した|しない|しなかった|できる|できない|できた|できなかった|"
    r"がある|がない|はない|もない|に限る|に近い|"
    r"なる|なった|ならない|ならなかった|"
    r"いる|いた|いない|ある|ない|あった|なかった|"
    r"要がある|欠かせない|"
    # う段で終わる動詞の終止形(行う・使う・扱う・持つ・呼ぶ・済む・読む など)
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:行|使|扱|担|補|問|奪|失|漂|伴|従|狙|願|養|補|覆|争|競)う|"
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:持|立|保|待|打|勝|育)つ|"
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:呼|運|選|並|学|結)ぶ|"
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:済|読|進|組|挟|含|生|住|望|絡)む|"
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:効|届|働|続|置|書|防|欠|引|開|巻|向|傾|省|履|貫)く|"
    r"(?<=[ぁ-んァ-ヶ一-龥])(?:示|残|返|渡|課|外|移|explicit)す|"
    r"(?:難し|速|遅|高|安|多|少な|良|よ|強|弱|近|widely)い"
    r")$")

def strip_deco(s):
    prev = None
    while prev != s:
        prev = s
        s = DECO.sub("", s).rstrip()
    return s


# 許容(文体の問題ではないもの)。内容の一部で照合する。
ALLOW = [
    # 「次の場合は Crossplane が向きます:」に続く条件の列挙
    "既に Crossplane に投資している",
    # AI に渡す指示文そのもの。地の文ではないので命令形のまま
    "その他」を判定する",
    "そう判断した根拠を1文で返す",
    "確信が持てない語は `uncertain` に残す",
    # 括弧内の注記。文末は括弧の外にある
    "残っていなければ呼ばれていない)",
    "**(必ず実行される)",
    # 箇条書きの太字ラベル(括弧を含むため fullmatch で落ちない)
    "永続ストレージの要求)に置く**",
    "**JetStream を耐久バッファとして挟む**",
]

hits = []
for path in sorted(pathlib.Path("articles").glob("*.md")):
    for lineno, ln in body_lines(path):
        text = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", ln).rstrip()
        # 「。」で終わる文、および行末が「:」「：」の行(脚注付きの導入文が該当)
        cands = []
        for m in re.finditer(r"[^。]+。", text):
            cands.append(m.group(0)[:-1])
        tail = re.split(r"。", text)[-1]
        if tail.strip():
            cands.append(re.sub(r"[:：]\s*$", "", tail))
        for c in cands:
            c0 = c.strip()
            # 見出し句(全体が **…** で囲まれた短い句)は文体の対象外
            if re.fullmatch(r"\*\*[^*]+\*\*", c0): continue
            # 括弧の中だけで終わる注記は対象外
            if re.search(r"[((][^)())]*$", c0): continue
            c2 = strip_deco(c.rstrip())
            c2 = re.sub(r"[:：]\s*$", "", c2).rstrip()
            c2 = strip_deco(c2)
            if DEARU.search(c2):
                if any(a in ln for a in ALLOW): continue
                hits.append((path.name, lineno, ln.strip()))

seen = set()
for f, l, s in hits:
    if (f, l) in seen: continue
    seen.add((f, l))
    print(f"{f}:{l}\n    {s[:150]}")
print(f"\n--- {len(seen)} 箇所 / {len(set(f for f,_,_ in hits))} 本 ---")

import sys
sys.exit(1 if seen else 0)
