# OpenClaude

claude-agent-sdk を使った常駐型 AI エージェントシステム。
Unix ソケットサーバーとして常駐し、セッション履歴を保持したまま会話を継続できます。

---

## 動作環境

- OS: Ubuntu 24.04 (WSL2)
- Python: 3.10 以上
- `claude-agent-sdk` v0.1.48 以上

---

## セットアップ

### 1. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 2. 設定ファイルの確認

`.claude/settings.json` に AWS Bedrock の認証情報とモデル設定が記載されています。
`setting_sources=["project"]` によって、デーモン起動時にこのファイルが自動的に読み込まれます。

```
.openclaude/
└── .claude/
    └── settings.json   # AWS_BEARER_TOKEN_BEDROCK, CLAUDE_CODE_USE_BEDROCK, model 等
```

### 3. PATH の設定

```bash
echo 'export PATH="$HOME/.openclaude:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 4. systemd サービスの登録（任意）

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
openclaude sessions
```

```
🦀 OpenClaude

Session store: /home/is0383kk/.openclaude/sessions/sessions.json
Sessions listed: 1

session-id  Age      Model                    Tokens (ctx %)
main        12m ago  claude-sonnet-4-6        7.7k/200k (3%)
```

### セッションデータの保存場所

| ファイル | 内容 |
|---|---|
| `sessions/sessions.json` | 全セッションのメタデータ（モデル・トークン数・最終更新時刻） |
| `sessions/{session-id}.jsonl` | セッションごとの会話ログ（JSONL 形式） |

---

## ログの確認

デーモンの動作ログは以下のファイルに出力されます。

```bash
cat ~/.openclaude/daemon.log
```

---

## アーキテクチャ

```
openclaude CLI
    │
    │  JSON over Unix Socket
    │  (~/.openclaude/openclaude.sock)
    ▼
OpenClaude Daemon (asyncio)
    │
    │  claude_agent_sdk.query()
    │  setting_sources=["project"]
    │  resume=sdk_session_id
    ▼
claude-agent-sdk → AWS Bedrock (Claude)
```

- **CLI** がメッセージをソケット経由でデーモンに送信
- **デーモン** が `claude-agent-sdk` を呼び出し、ストリーミングでレスポンスを返却
- セッション ID を保存することで、次回以降の会話を `resume` オプションで再開
