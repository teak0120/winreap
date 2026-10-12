# winreap

[English](README.md)

AI コーディングエージェント（Claude Code、Codex、MCP のホスト、IDE）が **Windows** に置き去りにした
補助プロセスを片付けます。

## なぜ要るか

Windows では、親が終わっても子は一緒に終わりません。SIGHUP も、PID 1 への付け替えもありません。
親が子を Job Object（`KILL_ON_JOB_CLOSE`）に入れていない限り、背景の `bash`、`tail -F`、
`grep --line-buffered`、MCP サーバーなどはエージェントの終了後も動き続けます。Claude Code と Codex
の Windows 版で、この報告が多数あります。

既存の片付けツール（cc-reaper、devreap など）は macOS/Linux 向けで、`PPID == 1` を探します。
Windows ではこれが起きないので使えません。winreap は Windows 向けに作っています。

## 止める物

次を**全部**満たす物だけが候補です。

1. **親が死んでいる** — 親の PID が無い、またはその PID を今持つプロセスが子より*後に*作られた
   （起動時刻を比べて PID の使い回しを見分ける）。親の起動時刻が読めない時は、生きているとみなす。
2. **十分古い** — `--min-age` 時間以上（既定 2。`--kill` の時は 2 未満を断る）。
3. **保護されていない**うえで、次のどちらか
   - コマンドラインが**許可パターン**に一致する（下の組み込みと、設定ファイルの分）
   - **一度も動いていない**: 全スレッドが一時停止・CPU 時間がちょうど 0・winreap と同じセッション。
     一時停止のまま作られ、再開される前に親が死んだ物で、命令を 1 つも実行していません。

安全のために:

- **既定は dry-run**。`--kill` を付けない限り何も止めません。
- `TerminateProcess` の直前に、同じハンドルで起動時刻を照合し直します。一度も動いていない物は、
  CPU 時間がまだ 0 であることも確かめ直します。
- `taskkill` もプロセスツリーごとの停止も使わず、1 本ずつ止めます。
- 許可パターンは、コマンドラインの先頭のプログラムに当てます。引数の中の同じ文字列には当てません。
- 管理者として `--kill` を実行すると、他のユーザーのプロセスに届くので断ります。必要なら
  `--allow-elevated` を付けてください。`install-task.ps1` は一般ユーザー（昇格なし）でタスクを登録します。

組み込みの許可パターン: `tail -F`、`grep -E --line-buffered`、Claude Code の shell-snapshot 付き
`bash -c`、1 回きりの `codex --version` / `codex debug models`、`cmd /c taskkill`、`@playwright/mcp`。
組み込みの保護パターン: `app-server`、`daemon run`。

## 使い方

Windows 上の Python 3.8 以上が要ります。標準ライブラリだけで動きます。

```powershell
python winreap.py                 # dry-run: 候補を表示するだけ
python winreap.py --kill          # 止める（記録は %LOCALAPPDATA%\winreap\winreap.log）
python winreap.py --config my.json --kill
python winreap.py --no-never-ran  # 許可パターンの規則だけ使う
```

タスクスケジューラで 1 時間ごとに動かす:

```powershell
.\install-task.ps1                       # PATH 上の python を使う（隣に pythonw.exe があればそちら）
.\install-task.ps1 -Config .\my.json
.\uninstall-task.ps1
```

## 設定

[`winreap.example.json`](winreap.example.json) を参照してください。

| キー | 意味 |
|---|---|
| `allow` | 追加の `[名前, 正規表現]`。コマンドライン全体に大文字小文字を区別せず当てる |
| `protect` | 追加の正規表現。一致した物は止めない（最初に見る） |
| `replace_builtin` | `true` で組み込みの許可・保護パターンを使わない |
| `never_ran` | `false` で「一度も動いていない」規則を切る |
| `log` | `--kill` の記録先 |

import して、自分のパターンを渡すこともできます。

```python
import winreap
winreap.main(allow=winreap.DEFAULT_ALLOW + [("mine", r"myhelper\.exe")],
             protect=winreap.DEFAULT_PROTECT + [r"keep-me"])
```

## 試験

```powershell
python -m unittest test_winreap
```

## ライセンス

MIT
