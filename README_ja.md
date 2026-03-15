<table>
	<thead>
    	<tr>
      		<th style="text-align:center"><a href="./README.md">English</a></th>
          <th style="text-align:center"><a href="./README_cn.md">Chinese</a></th>
      		<th style="text-align:center">日本語</th>
    	</tr>
  	</thead>
</table>

# 🦀OpenClaude — Claude Code-native personal AI assistant

`claude-agent-sdk` を使った常駐型 AI エージェントシステム。Claude Codeの`settings.json`をベースに動作します。  
このプロジェクトは [OpenClaw](https://github.com/openclaw/openclaw) に触発されたプロジェクトです。  
Unix ソケットサーバーとして常駐し、CLI・REST API からメッセージを受け付けて Claude にプロキシします。

---

## 機能一覧

| 機能                                 | コマンド / エンドポイント                                   |
| ------------------------------------ | ----------------------------------------------------------- |
| デーモン起動・停止・再起動・状態確認 | `openclaude start/stop/restart/status`                      |
| メッセージ送信（ストリーミング）     | `openclaude -m "メッセージ"`                                |
| stdin / パイプ入力                   | `echo "質問" \| openclaude`                                 |
| ログ表示                             | `openclaude logs [--tail N]`                                |
| セッション管理                       | `openclaude sessions`                                       |
| Cron ジョブ管理                      | `openclaude cron add/list/delete/run`                       |
| HTTP REST API                        | `POST /message`, `POST /message/stream`, `GET /status` など |
| Cron REST API                        | `GET /cron`, `POST /cron`, `DELETE /cron/{id}` など         |

---

## セットアップ

### 前提

- Linux／Windows（WSL2）
- Python >= 3.14
- [claude-agent-sdkが利用できる環境](https://platform.claude.com/docs/ja/agent-sdk/overview)

### 依存ライブラリ

| パッケージ                 | 用途                            |
| -------------------------- | ------------------------------- |
| `claude-agent-sdk>=0.1.48` | Claude AI エージェント SDK      |
| `fastapi>=0.115.0`         | REST API フレームワーク         |
| `uvicorn>=0.30.0`          | ASGI サーバー                   |
| `apscheduler>=3.10,<4`     | Cron ジョブスケジューラ（v3.x） |

### インストール

```bash
git clone <repository-url> ~/.openclaude
cd ~/.openclaude
pip install -r requirements.txt
```

> **注意:** プロジェクトは必ず `~/.openclaude/` に配置してください。
> `src/config.py` が `Path.home() / ".openclaude"` をベースパスとして使用するため、別ディレクトリでは動作しません。

---

## 使い方

### デーモン管理

```bash
# 起動（デフォルトポート: 28789）
openclaude start

# ポートを指定して起動
openclaude start --port 18789

# 停止
openclaude stop

# 再起動
openclaude restart

# 状態確認
openclaude status

# ログ表示
openclaude logs           # 全内容
openclaude logs --tail 50 # 末尾50行
```

### メッセージ送信

```bash
# シンプルな送信
openclaude -m "プロンプト"

# セッションを指定
openclaude --session-id work -m "プロンプト"

# stdin / パイプ
echo "質問" | openclaude
cat report.txt | openclaude -m "これを要約して"
git diff | openclaude -m "このdiffをレビューして"
```

### セッション管理

```bash
# 一覧表示
openclaude sessions

# 全セッション削除
openclaude sessions cleanup

# 特定セッション削除
openclaude sessions delete <session-id>
```

### Cron ジョブ

```bash
# ジョブ追加（毎朝9時に実行）
openclaude cron add "0 9 * * *" --name "morning" --session main -m "今日のタスクを整理して"

# 一覧表示
openclaude cron list

# 手動実行
openclaude cron run <job-id>

# 削除
openclaude cron delete <job-id>
```

### systemd 連携（セットアップ済みの場合）

```bash
systemctl --user start openclaude
systemctl --user stop openclaude
systemctl --user status openclaude
```

---

## REST API

デーモン起動後、デフォルトで `http://localhost:28789` でアクセスできます。

| メソッド | パス              | 説明                                 |
| -------- | ----------------- | ------------------------------------ |
| `POST`   | `/message`        | メッセージ送信（完全レスポンス）     |
| `POST`   | `/message/stream` | メッセージ送信（SSE ストリーミング） |
| `GET`    | `/status`         | デーモンステータスと PID             |
| `GET`    | `/sessions`       | セッション一覧                       |
| `DELETE` | `/sessions`       | 全セッション削除                     |
| `DELETE` | `/sessions/{id}`  | 指定セッション削除                   |
| `GET`    | `/cron`           | Cron ジョブ一覧                      |
| `POST`   | `/cron`           | Cron ジョブ追加                      |
| `DELETE` | `/cron/{id}`      | Cron ジョブ削除                      |
| `POST`   | `/cron/{id}/run`  | Cron ジョブ手動実行                  |

---

## アーキテクチャ

```
CLI (openclaude)
  └── src/cli.py
        └── Unix ソケット (~/.openclaude/openclaude.sock) 経由でデーモンに通信

デーモン + API サーバー（同一プロセス）
  ├── src/daemon.py  ── Unix ソケットサーバー
  ├── src/api.py     ── FastAPI + uvicorn（REST API）
  └── src/cron.py    ── apscheduler によるスケジューラ
```

### ファイル構成

```
~/.openclaude/
  ├── src/
  │   ├── config.py    # ファイルパス定数・ロギング設定
  │   ├── daemon.py    # Unix ソケットサーバー・メッセージハンドラー
  │   ├── api.py       # FastAPI REST API サーバー
  │   ├── cron.py      # Cron ジョブ管理（CronJob / CronScheduler）
  │   └── cli.py       # CLI エントリーポイント
  ├── sessions/
  │   └── sessions.json         # セッションエイリアス → SDK セッション ID マッピング
  ├── cron/
  │   ├── jobs.json             # Cron ジョブ定義（永続化）
  │   └── runs/<job_id>.jsonl   # 実行履歴
  ├── openclaude.sock           # Unix ソケット（起動中のみ）
  ├── openclaude.pid            # PID ファイル（起動中のみ）
  └── daemon.log                # デーモンログ
```
