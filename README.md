# investment_agent

AI エージェントが自動売買する様子をリアルタイムダッシュボードで観察するシステム(Phase 1: ペーパートレード)。

- 仕様の正: [`docs/requirements.md`](docs/requirements.md)
- デザインの正: [`docs/dashboard-mockup.html`](docs/dashboard-mockup.html)
- 開発ルール: [`CLAUDE.md`](CLAUDE.md)

## 構成

```
core/        # エージェント・コア(Python 3.12 / asyncio)
             #   feed(bitFlyer 公開 WS / sim)→ market → agent(Claude / mock)
             #   → risk gate(コード強制)→ paper broker → store(SQLite)→ WS push
dashboard/   # ダッシュボード(TypeScript strict + Vite、Canvas 自前描画)
docs/        # 要件定義書・デザインモック
tests/       # core のテスト(リスクゲート境界値・キルスイッチ・ブローカー・LLM モック等)
```

## セットアップ

```bash
# Python(3.12+、uv 推奨)
uv venv --python 3.12 && uv sync --all-groups

# ダッシュボード
cd dashboard && npm install && npm run build && cd ..
```

## 起動

```bash
# 本番相当: bitFlyer 公開 WebSocket + Claude 実判断(要 ANTHROPIC_API_KEY)
cp .env.example .env   # 必要な値を設定
.venv/bin/python -m core.main

# 開発・検証: シミュレーションフィード + モック判断(API キー不要)
.venv/bin/python -m core.main --feed sim --llm mock --cycle 15
```

ブラウザで `http://127.0.0.1:8765/` を開く(`dashboard/dist` があればコアが配信)。
開発時は `cd dashboard && npm run dev` で Vite dev server(:5173)からも接続できる
(/ws・/api はコアへプロキシ)。

- **緊急停止**は画面右上のボタン。状態はコア側(SQLite)に保持され、
  ダッシュボードを閉じても・コアを再起動しても維持される
- `IA_AUTH_TOKEN` を設定すると WS / API に認証が必要になる(`?token=` を URL に付与)

## 品質ゲート

```bash
.venv/bin/ruff check .          # lint
.venv/bin/mypy core tests       # 型(strict)
.venv/bin/pytest                # テスト(リスクゲート・キルスイッチ・ブローカー等)
cd dashboard && npx tsc --noEmit  # TS 型チェック
```

ダッシュボード変更時は実ブラウザ(Playwright, `deviceScaleFactor: 2`)で
数十秒稼働させてレイアウト安定性を確認すること(CLAUDE.md 品質ゲート 4)。
