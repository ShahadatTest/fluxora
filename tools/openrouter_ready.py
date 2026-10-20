"""Inspect local OpenRouter setup without network access or printing a key."""
import json
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter, OpenRouterAdapter


def main():
    adapter = OpenAICompatibleAdapter.from_env()
    report = dict(
        mode='local configuration only; zero provider calls',
        openrouter_profile=isinstance(adapter, OpenRouterAdapter),
        state=adapter.status().state,
        provider_label=adapter.provider_label,
        destination=adapter.status().base_url,
        key_present=adapter.is_configured,
        model_present=bool(adapter.status().model),
        request_policy=adapter.request_policy(),
        live_verification='pending; no network request made',
    )
    print(json.dumps(report, indent=2))
    return 0 if report['openrouter_profile'] and report['model_present'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
