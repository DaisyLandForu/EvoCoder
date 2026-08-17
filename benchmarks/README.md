# Benchmarks

This directory is the **reproducible** evaluation surface for EvoCoder. It does not replay historical GAIA / HLE scores. Those numbers are not produced by this runner and must not be cited as current results.

## Layout

```text
benchmarks/
├── configs/ci.json
├── tasks/software_engineering_mini.jsonl
├── manifests/ci.manifest.json
└── runner/
runs/<run_id>/
├── config.json
├── manifest.json
├── results.jsonl
├── traces/
├── artifacts/
└── summary.json
```

A run freezes dataset version, task IDs, model, provider, temperature, budget, tool set, Skill version, memory/folding flags, timeouts, seed, and Git SHA.

## Command

```bash
python -m benchmarks.runner
```

CI uses the scripted OpenAI backend. It does not need a real API key and does not drop failed tasks. `se-wrong-output` is kept as an unsuccessful sample on purpose.

## Metrics

`summary.json` reports task success rate, average tool steps, P95 latency, average tokens/cost, hard failures, NTR, and three-arm paired deltas from the bundled Skill shadow evaluation.
