# Load test: usage pipeline on OCI

**Setup.** OKE in `ap-mumbai-1` on **one VM.Standard.E5.Flex node (1 OCPU = 2 vCPU, 16 GB)**. The
load generator, the collector (1 pod), the aggregator (2 pods) and Valkey all share that node.
OCI Streaming `usage-events` has 4 partitions. The traffic is synthetic mediation batches from
100,000 subscribers (75% data, 20% voice, 5% SMS records). Everything goes through the real path:
HTTP batch → collector (validate, batch-level idempotency, partition, gzip) → OCI Streaming
(Kafka API, SASL_SSL) → aggregator (atomic Lua apply, exactly-once) → Valkey.

Reproduce: `deploy/loadtest/job.yaml` (set `--rate`).

| Run | Target | Achieved | Records | Retries / dupes | Drain after generator stopped |
|---|---|---|---|---|---|
| 1 | 3,000 rec/s × 120 s | **2,990 rec/s** | 359,250 | 0 / 0 | ≈ 0 s (no backlog) |
| 2 | 15,000 rec/s × 120 s | **14,721 rec/s** | 1,770,000 | 0 / 0 | ≈ 20 s |

Peak CPU in run 2: collector ≈ 0.32 cores, aggregators ≈ 0.41 + 0.31 cores, load generator ≈ 0.19
cores. The single node's CPU is now the limit, not the design. Valkey used 14 MB.

## What this says about 60k/s average and 150k/s peak

| Dimension | Measured | Production peak (150k rec/s) |
|---|---|---|
| Aggregator CPU | ≈ 0.72 cores per 15k rec/s | ≈ 7–8 cores → 10–12 pods (one or more partitions each) |
| Collector CPU | ≈ 0.3 cores per 15k rec/s | ≈ 3 cores → 4–6 pods behind HPA |
| Wire bytes / partition | 200 B raw × 15k ≈ 3 MB/s raw, ≈ 0.6 MB/s gzip over 4 partitions (≈ 0.15 MB/s each) | 30 MB/s raw ≈ 6 MB/s gzip → **8 partitions at 75% of the 1 MB/s limit; provision 12–16** |
| Valkey | single shard, mostly idle | Cluster mode; each partition's keys are one hash slot (`{pN}`), so the Lua apply stays atomic per shard |
| Consumer scaling | pods ≤ partitions | Partition count is fixed at stream creation on OCI → **size it for peak + headroom on day 1** |

The design is horizontally scalable along the partition axis. Every component that does work is
either stateless (collector) or owns a disjoint set of partitions (aggregator, Valkey shard).
There's no global lock, no cross-partition transaction, and no SUM over raw rows anywhere.

## Next steps a production rollout would add
- KEDA autoscaling on consumer lag (stream offset minus the Valkey `off:{pN}` key) instead of CPU.
- A dedicated node pool for the hot path so it can't be squeezed by API pods.
- A multi-hour soak test and a chaos test: kill an aggregator mid-batch, then verify totals and
  alert counts against the raw archive (the exactly-once claim, measured).
