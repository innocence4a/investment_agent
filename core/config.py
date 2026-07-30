"""設定。環境変数(接頭辞 IA_)で上書き可能。

リスク制約のデフォルトはここで定義するが、制約の強制そのものは risk.py が行う。
秘密情報(ANTHROPIC_API_KEY / SLACK_WEBHOOK_URL)は環境変数でのみ受け取り、
リポジトリ・DB に保存しない。
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskConfig(BaseModel):
    """リスクゲート設定(F-3)。変更には発注者の明示的承認が必要。"""

    max_trade_notional_jpy: int = 50_000  # 1 取引の上限(想定元本)
    max_daily_loss_jpy: int = 30_000  # 日次損失上限(JST 日で集計、実現損益)
    max_exposure_jpy: int = 200_000  # 総エクスポージャ上限(全ポジション評価額)
    max_trades_per_day: int = 30  # 取引頻度上限(JST 日・約定ベース)
    stop_loss_pct: float = 2.0  # 損切りライン(取得単価比 %)。コード側で常時監視
    take_profit_pct: float = 4.0  # 利確ライン(取得単価比 %)


class RiskAgentConfig(BaseModel):
    """リスク管理エージェント(F-20)のルール設定。

    抑制の判定はすべてルール(コード)で行い、結果はリスクゲートの状態として強制される。
    """

    vix_size_half: float = 25.0  # VIX がこの値以上 → サイズ半減
    vix_no_entry: float = 35.0  # VIX がこの値以上 → 新規停止
    consecutive_losses_size_half: int = 3  # 本日この連敗数以上 → サイズ半減
    daily_loss_warn_ratio: float = 0.8  # 日次損失が上限のこの割合以上 → サイズ半減
    check_interval_sec: float = 30.0  # 定期評価の間隔


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IA_", env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    # モード(Phase 1 は paper 固定。live は Phase 2 で発注者承認後にのみ実装)
    mode: str = "paper"

    # フィード: "bitflyer"(実データ) / "sim"(開発・検証用シミュレーション)
    feed: str = "bitflyer"
    # LLM: "anthropic"(実 API) / "mock"(開発・検証用ルールベース)
    llm: str = "anthropic"

    start_capital_jpy: int = 1_000_000
    decision_interval_sec: float = 300.0  # LLM 判断サイクル(推奨 5 分)
    slippage_bps: int = 5  # ペーパー約定の想定スリッページ(0.05%)
    fee_bps: int = 15  # 想定手数料(bitFlyer 現物 0.15% 相当)

    trader_model: str = "claude-sonnet-5"
    llm_timeout_sec: float = 30.0
    llm_max_retries: int = 2
    llm_monthly_cost_limit_usd: float = 50.0  # 月次コスト上限(超過時は判断を停止)

    db_path: str = "data/agent.db"
    host: str = "127.0.0.1"
    port: int = 8765
    # ダッシュボード認証トークン(N-4)。設定時は WS / API に必須
    auth_token: str = ""

    slack_webhook_url: str = ""
    feed_stale_sec: float = 60.0  # この秒数ティックが無ければフィード断として通知

    # ── Phase 1.5 ──
    # 関連指標(F-18): "auto"(feed=sim なら sim、それ以外は実ソース)/ "real" / "sim"
    macro_source: str = "auto"
    macro_poll_sec: float = 300.0  # 無料ソースは 15〜20 分遅延のため 5 分ポーリングで十分
    macro_stale_sec: float = 1800.0  # これより古い指標は判断材料から外す
    # 経済指標カレンダー(F-19): 空なら同梱の core/data/economic_calendar.json
    calendar_path: str = ""
    # 相談役(F-21): 日次レビューの実行時刻(JST)とモデル
    # 範囲外の値は起動時にエラーにする(不正値で advisor ループが連続クラッシュしない)
    advisor_hour_jst: int = Field(default=7, ge=0, le=23)
    advisor_model: str = "claude-sonnet-5"

    risk_agent: RiskAgentConfig = RiskAgentConfig()
    risk: RiskConfig = RiskConfig()
