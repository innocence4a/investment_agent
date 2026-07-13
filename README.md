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

## 起動後の動作確認チェックリスト

まず検証モード(`--feed sim --llm mock --cycle 15`)で以下を確認する。

### 基本動作
- [ ] ヘッダに **PAPER** バッジが常時表示されている(検証モードでは「SIM データ」「モック判断」バッジも)
- [ ] 時刻(JST)が毎秒更新され、KPI の「稼働時間」が進む
- [ ] チャートの価格が動き、ローソク足が形成されていく(タイムフレームタブ・BTC/ETH タブが切替可能)
- [ ] **思考ログに判断サイクル毎(`--cycle` 秒毎)にカードが追加される** — 見送り判断にも日本語の根拠と確信度が付いていること(全判断記録の要件)
- [ ] 売買が発生したら: チャートに ▲▼ マーカー / 約定フィードに行が追加 / ポジション表・KPI(総資産・損益)が更新される

### 緊急停止(F-13)— 安全機能なので必ず確認
- [ ] 「緊急停止」ボタン → 確認ダイアログ → 表示が停止中に変わり、思考ログにシステムカードが流れる
- [ ] 停止中は新規エントリーが発生しない(決済・損切り監視は継続)。`curl http://127.0.0.1:8765/api/health` が `"halted": true` を返す
- [ ] 「再開」で `"halted": false` に戻る
- [ ] **停止したままコアを再起動**(Ctrl-C → 再度起動)→ 停止状態が維持され、起動メッセージに「緊急停止状態を引き継ぎました」と出る
- [ ] コアを落とした状態で停止ボタンを押す → 画面上部に「⚠ 緊急停止リクエストが失敗しました…」のエラーバナーが表示される(サイレント失敗しない)

### 本番相当(bitFlyer 実データ+Claude 実判断)
- [ ] `.env` に `ANTHROPIC_API_KEY` を設定し、引数なしで起動 → ヘッダのバッジが実データ・実判断表示になる
- [ ] BTC/ETH の表示価格が実勢と一致する(bitFlyer の公式サイトと照合)
- [ ] 判断サイクル毎に Claude の判断(具体的な指標に言及した日本語根拠)が思考ログに流れる
- [ ] KPI の月次 LLM コストが呼び出し毎に増えていく(`IA_LLM_MONTHLY_COST_LIMIT_USD` 到達で判断サイクルが自動停止する)
- [ ] Slack Webhook を設定した場合: 起動・約定・ガードレール発動の通知が届く

## 品質ゲート

```bash
.venv/bin/ruff check .          # lint
.venv/bin/mypy core tests       # 型(strict)
.venv/bin/pytest                # テスト(リスクゲート・キルスイッチ・ブローカー等)
cd dashboard && npx tsc --noEmit  # TS 型チェック
```

ダッシュボード変更時は実ブラウザ(Playwright, `deviceScaleFactor: 2`)で
数十秒稼働させてレイアウト安定性を確認すること(CLAUDE.md 品質ゲート 4)。
