# External evaluation on Cloud-OpsBench

[Cloud-OpsBench](https://github.com/LLM4Ops/Cloud-OpsBench) ([arXiv 2603.00468](https://arxiv.org/abs/2603.00468), MIT) is a published benchmark of 754 recorded Kubernetes incidents. Agents investigate through replayed diagnostic tools, so it runs offline, with no cluster. It scores root cause analysis only: there is no remediation task.

## What was compared

There were two agents. Everything was identical between them except Nabd's method:
- The same model: `deepseek-flash` through DeepSeek's OpenAI-compatible API, at temperature 0 with 4096 max tokens.
- The same tools, the same prompt and the same budget of 20 steps.

| Agent | What it is |
|---|---|
| reference | The benchmark's own ReAct agent (`agents/cloudops_agent`), unchanged |
| nabd | The same agent plus Nabd's two diagnosis-side checks. (1) A **validity gate**: each predicted root cause must target the object kind it requires. (2) A **skeptic review** before a submission is accepted: a separate model call tries to refute rank 1, and a critique sends the agent back to investigate within the same step budget. |

The reference agent was run twice, to measure run-to-run noise.

**Sample:** 40 Online Boutique cases, 5 per fault category. They were drawn with `random.Random(2026)` and written to `results/cases-preregistered.json` before any run (`nabd_runner/select_cases.py`). Scoring uses the benchmark's own `evaluate_case`.

## Results

| | reference (run 1) | reference (run 2) | nabd |
|---|---|---|---|
| Root cause and object correct (JRA) | 39/40 (97.5%) | 37/40 (92.5%) | 37/40 (92.5%) |
| Evidence closure (ECR) | 57.5% | 67.5% | 75.0% |
| Milestone coverage (MC) | 0.83 | 0.87 | 0.88 |
| Tool steps | 6.0 | 6.3 | 6.8 |
| Model calls per case | 6.3 | 6.6 | 8.9 |
| Output tokens per case | 3.0k | 3.4k | 12.1k |

Paired sign tests on the same cases:
- **Accuracy:** no difference. Nabd matches reference run 2 on every case. Against run 1 it is worse on 2 and better on 0 (p = 0.5).
- **Evidence closure:** Nabd is better than run 1 on 8 cases and worse on 1 (p = 0.039). Against run 2 it is better on 4 and worse on 1 (p = 0.375). The two reference runs alone differed on 10 cases (7 to 3). **The evidence gain is within run-to-run noise, so it is not claimed.**

The skeptic upheld 30 submissions, asked for one more check 15 times, refuted 1, and failed to answer once. The validity gate fired once.

## What this means

On this benchmark, today's model already gets the diagnosis right about 93 to 97% of the time. The published figure for the earlier DeepSeek-V4-Flash was 76%. Nabd's diagnosis-side checks add cost without a measurable gain. That matches R2Act's finding: diagnosis is no longer the bottleneck, and choosing a valid recovery is. Cloud-OpsBench does not measure recovery. Nabd's recovery layers (validity gate on actions, dry run, verification) were exercised only on Nabd's own live system. AIOpsLab's mitigation task is the external test for them, and it is not done yet.

An earlier run is kept in `runs/nabd_v1_broken_skeptic` and is **not** a valid result. In that run the skeptic's output hit the 4096-token limit, and 30 of 39 reviews were unreadable. The code silently counted them as "upheld". The fix: a larger budget for the skeptic, one retry, a raw-text log, and a `review_failed` verdict instead of a silent pass.

## Reproduce

```bash
python -m venv eval/.venv
eval/.venv/Scripts/pip install -r eval/cloud-opsbench/agents/cloudops_agent/requirements.txt
cd eval/nabd_runner
../.venv/Scripts/python run_compare.py --agent reference --workers 4
../.venv/Scripts/python run_compare.py --agent nabd --tag nabd_v2 --workers 4
../.venv/Scripts/python score.py reference nabd_v2
```

The benchmark repository is cloned into `eval/cloud-opsbench` (3.9 GB with case data). The DeepSeek key is read from `NABD_LLM_API_KEY` in `.env`.
