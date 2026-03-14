> [!WARNING]
> このリポジトリは開発途中です。ソースコードや各種ドキュメントは不完全です。  
> This repository is under development. The source code and documentation are incomplete.

# OpenClaude

claude-agent-sdk を使った常駐型 AI エージェントシステム  
Unix ソケットサーバーとして常駐し OpenClaw のように24時間稼働し続けます

---

## 動作環境

- OS: Linux（Ubuntu 24.04で動作確認済み）
- Python: 3.10 以上
- `claude-agent-sdk` v0.1.48 以上

---

## セットアップ

### 1. プロジェクトの配置

`/home/ユーザ名/.openclaude`となるようにプロジェクトを配置する

### 2. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 3. 設定ファイルの確認

`.claude/settings.json` に 必要な項目を追記  
`ClaudeAgentOptions`の`setting_sources=["project"]` により、デーモン起動時にこのファイルが自動的に読み込まれます。

```json
.openclaude/
└── .claude/
    └── settings.json
```

### 4. PATH の設定

```bash
echo 'export PATH="$HOME/.openclaude:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 5. systemd サービスの登録（任意）

`systemctl` コマンドで起動・停止したい場合は以下を実行してください。  

```bash
mkdir -p ~/.config/systemd/user
cp ~/.openclaude/openclaude.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable openclaude.service
```

> **注意**: `openclaude.service` 内の `ExecStart` に記載された `python3` が
> `claude-agent-sdk` をインストールした Python であることを確認してください。
> `which python3` で確認できます。

---

## プロジェクト構成

```
.openclaude/
├── openclaude              # CLI 実行スクリプト (chmod +x)
├── openclaude.service      # systemd ユーザーサービス テンプレート
├── requirements.txt
├── .claude/
│   └── settings.json       # AWS Bedrock 設定（認証情報・モデル）
├── src/
│   ├── __init__.py
│   ├── config.py           # パス定数
│   ├── session_store.py    # セッション永続化
│   ├── daemon.py           # Unix ソケットサーバー本体
│   └── cli.py              # CLI コマンド実装
└── sessions/               # セッションデータ（初回起動時に自動生成）
    ├── sessions.json       # セッションメタデータ一覧
    └── {session-id}.jsonl  # セッションごとの会話履歴
```

---

## 基本コマンド

### デーモンの起動・停止

```bash
# 起動
openclaude start

# 停止
openclaude stop

# 再起動
openclaude restart

# 状態確認
openclaude status
```

systemd 経由でも操作できます。

```bash
systemctl --user start openclaude
systemctl --user stop openclaude
systemctl --user restart openclaude
systemctl --user status openclaude
```

---

### ログの確認

デーモンのログ（`~/.openclaude/daemon.log`）を表示します。

```bash
# 全ログを表示
openclaude logs

# 末尾 N 行のみ表示
openclaude logs --tail 50
```

---

## 会話セッションの使い方

### メッセージを送る

```bash
openclaude --session-id main --message "プロンプト"
```

`--session-id` を省略すると `main` が使用されます。

```bash
openclaude --message "プロンプト"
```

短縮形 `-m` も使えます。

```bash
openclaude -m "プロンプト"
```

### 実行例

```
$ openclaude --session-id main --message "こんにちは、私はis0383kkです"

🦀 OpenClaude（main）
│
◇
こんにちは、is0383kk さん！何かお手伝いできることはありますか？
```

### セッションの継続

同じ `--session-id` を指定すると、前回の会話コンテキストが引き継がれます。

```bash
openclaude --session-id main --message "私の名前はなんでしたか？"
# → "is0383kk さん" と答えてくれる
```

---

## セッション管理

### セッション一覧の確認

```bash
$ openclaude sessions
🦀 OpenClaude

Sessions: 1
Sessions Path: /home/ユーザ名/.claude/projects/-home-ユーザ名--openclaude

session-id  sdk_session_id                        last_active               total_tokens
main        XXXXXXXXXXXXXXXXXXX  2026-03-13T17:49:54.126Z  5743
```

### セッションデータの保存場所

| ファイル                      | 内容                                                         |
| ----------------------------- | ------------------------------------------------------------ |
| `sessions/sessions.json`      | Claude側で管理されているセッション情報と紐づくセッションID |
