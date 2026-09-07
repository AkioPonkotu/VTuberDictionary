# VTuber Dictionary

日本の VTuber 名を読み仮名から正式名称へ変換できる IME 辞書を、安全に生成・更新する Python パイプラインです。

> 現在は実装の土台を整備中です。AI が推測した読みをそのまま辞書に採用せず、独立した検証と決定論的な検査を通った情報だけを成果物に含めます。

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) to provision its own Python 3.12 environment and lock dependencies.

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run pytest
uv run ruff check .
uv run mypy src
```

Set only the credentials required by the stage you are running in `.env`. Never commit that file.

## Planned layout

```text
src/vtuber_dictionary/  Application package
tests/                  Unit tests with fake external clients
data/                   Versioned canonical data and local runtime state
dist/                   Generated IME dictionary artifacts
```

The pipeline will persist candidate identities between runs, use official YouTube and Twitch APIs for audience thresholds, and use separate research and verification agents before compilation.
