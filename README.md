# VTuber Dictionary

日本の VTuber 名を、ひらがなの読みから正式名称へ変換する UTF-8 IME 辞書を安全に更新する Python パイプラインです。AI の推測だけでは一切採用せず、公式情報の調査、独立した検証、決定論的な検査を全て通った項目だけを配布辞書へ入れます。

## アーキテクチャ

```text
AgencyDiscovery ─┐
                 ├─ CandidateRepository ─ identity resolution ─ platform metrics
TwitchDiscovery ─┘                                         │
      Get Streams → VTuber tag                              ▼
       (live only)                              Threshold / existing-entry filters
                                                               ▼
                                                  ReadingResearchAgent
                                                               ▼
                                                   VerificationAgent
                                                               ▼
                                                   DeterministicValidator
                                                               ▼
                                                   DictionaryCompiler → dist TSV
```

`domain.py` は外部サービスから独立したデータ構造です。HTTP API と Agent は protocol/adapter を介し、テストでは fake に置換します。YouTube/Twitch の規模判定は通常の Python コードと公式 API の値だけで行い、LLM には委ねません。

## セットアップ

Python 3.12 と [uv](https://docs.astral.sh/uv/) が必要です。

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run pytest
uv run ruff check .
uv run mypy src
```

`.env` に次を設定します。絶対にコミットしません。

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Microsoft Agent Framework 経由の調査・検証 Agent |
| `YOUTUBE_API_KEY` | YouTube Data API のチャンネル統計・概要 |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Twitch Helix の発見・統計 |
| `YOUTUBE_MIN_SUBSCRIBERS` | 既定 10000 |
| `TWITCH_MIN_FOLLOWERS` | 既定 5000 |
| `AUDIENCE_THRESHOLD_MODE` | 既定 `any`（YouTube **または** Twitch）。`all` も利用可能 |
| `REVERIFY_AFTER_DAYS` | 既存エントリを再検証するまでの日数（既定 180） |
| `TWITCH_DISCOVERY_ENABLED`, `TWITCH_DISCOVERY_LANGUAGE`, `TWITCH_DISCOVERY_TAG`, `TWITCH_DISCOVERY_MAX_PAGES` | Twitch の収集範囲。language を空にすると言語制限なし |

事務所公式プロフィールから発見した候補は、規模にかかわらず調査・検証の対象です。規模閾値は Twitch 発見など、事務所に紐付かない候補にのみ適用します。

`uv sync` は Microsoft Agent Framework の OpenAI provider もインストールします。Agent は OpenAI の hosted Web Search tool と Pydantic response format を指定して実行します。

`OPENAI_MODEL` は必須です。利用するモデル名を環境変数またはGitHub Repository Variableに指定してください。

## 実行

事務所は `data/agencies.json` に登録します（初期状態は空です）。事務所をコードへ埋め込まないため、追加・無効化はこのデータだけで行えます。

```json
[
  {
    "name": "Example Agency",
    "official_url": "https://example.com",
    "talent_list_url": "https://example.com/talents",
    "profile_url_pattern": "^/talents/[^/]+/$",
    "active": true
  }
]
```

`profile_url_pattern` は任意です。タレント以外のナビゲーションリンクを候補にしないため、公式サイトのプロフィールURL形式が分かる場合は指定します。

```powershell
uv run vtuber-dictionary update
# 取得元を限定する場合
uv run vtuber-dictionary update --source agency
uv run vtuber-dictionary update --source twitch
```

`--source` は複数回指定できます。指定しない場合は従来どおり事務所・Twitch の両方を取得します。これは候補の**発見元**だけを限定するオプションで、既存候補の YouTube/Twitch 統計による検証は維持されます。

OpenAI の認証情報がない場合も、閾値に達した候補が現れるまで候補収集は実行できます。その候補を調査・採用する段階で停止するため、未検証の情報が辞書へ入ることはありません。

実行中の永続データは次のとおりです。

```text
data/agencies.json             Versioned agency registry
data/candidates.jsonl          Candidate identity cache (execution state)
data/entries.jsonl             Verified canonical data
data/review_required.jsonl     Rejected/ambiguous cases and reasons
dist/vtuber_dictionary.tsv     Distribution artifact
```

出力の各行は `reading<TAB>canonical_name` です。例: `ほしまちすいせい<TAB>星街すいせい`。`TsvExporter` は Google 日本語入力、Microsoft IME、macOS 向け exporter を追加できるよう、コンパイラから分離しています。

## 調査・検証ポリシー

ReadingResearchAgent は公式事務所プロフィール、本人公式サイト、YouTube/Twitch 概要、公式 SNS を順に優先して読みを調査します。VerificationAgent は別の instructions/context で同じ結論と URL を独立に確認します。公式根拠のない漢字・ローマ字からの推測は `unresolved` となり、採用されません。

同一人物の統合は YouTube channel ID、Twitch user ID、または公式プロフィール URL の一致だけで行います。表示名が一致するだけでは統合せず、曖昧なものは `review_required` です。既存エントリは canonical ID と再検証期限で判定し、無駄な Web Search / OpenAI 呼び出しを避けます。

Twitch Discovery は Helix の **Get Streams** をページングして `VTuber` タグ（大文字小文字を区別しない）を持つライブ配信者だけを発見します。ライブ中でない VTuber を一度に検索する仕組みではありません。そのため `candidates.jsonl` を実行をまたいで保持し、候補集合を少しずつ増やします。

## GitHub Actions

定期更新は次の二つの workflow に分かれています。いずれも `workflow_dispatch` で手動実行もできます。

| Workflow | スケジュール | 取得元 |
| --- | --- | --- |
| `.github/workflows/update-agencies.yml` | 毎週月曜 10:13 JST（01:13 UTC） | 登録済み事務所の公式プロフィール |
| `.github/workflows/update-twitch.yml` | 4時間ごと、毎時 :23 UTC | Twitch のライブ配信（`VTuber` タグ） |

二つの workflow は共通のキューで直列化されるため、同じ `data` と `dist` への競合コミットを避けます。辞書 TSV に変更があった実行だけが、日時と workflow run ID を含む一意なタグの GitHub Release を作成し、`vtuber_dictionary.tsv` を添付します。Secrets に `OPENAI_API_KEY`、`YOUTUBE_API_KEY`、`TWITCH_CLIENT_ID`、`TWITCH_CLIENT_SECRET` を設定してください。更新中の API エラーは失敗として終了するため、既存の `dist` を不完全な内容で上書きしません。

## テストと貢献

`tests/` は external API を使わない unit test です。Twitch follower API の opt-in smoke test は次で実行できます。

```powershell
$env:TWITCH_SMOKE_BROADCASTER_ID = "..."
uv run pytest tests/integration --override-ini="addopts=--strict-markers"
```

変更時は unit test、ruff、mypy を通し、公式 URL を保ったまま小さくレビューしやすい commit にしてください。API keys、client secret、`.env`、生成済みのローカル state を pull request に含めないでください。
