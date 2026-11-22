# CDR scenario coverage

Every row below is (a) produced by the network emulator (personas generate the common types continuously;
`network-emulator scenarios` replays each edge case on demand against the lab line +49 151 0000 0099),
(b) handled by a specific stage of the pipeline, and (c) covered by an automated test.

Run the scenarios against the live system:
```bash
REG=$(grep registry: deploy/helm/values-oci.yaml | awk '{print $2}')
sed -e "s|REGISTRY|$REG|" -e "s|IMAGE_TAG|latest|" deploy/loadtest/scenarios-job.yaml | kubectl -n selfcare apply -f -
kubectl -n selfcare logs -f job/cdr-scenarios
```
The System page shows the results live: CDR counts per type, quarantined and duplicate counts, and sample quarantine reasons (reasons only, no record ids).

## Record types

| Scenario | CDR | How it is handled | Billing rule | Test |
|---|---|---|---|---|
| Mobile data | `DATA` bytes | summed per cycle | per started MB beyond allowance (Flex) or throttle (S/M) | `test_flex_plan_charges_overage…`, `test_throttle_plan…` |
| Long data session (interim records) | `DATA` + `session_id` + `sequence` | each partial record summed; dedupe by `record_id` | as data | `test_every_scenario…` (data-session-interim) |
| Session across midnight or the cycle boundary | two partials either side of 00:00 on the 1st | assigned by **event time** → each part lands in its own cycle | as data | `test_new_cycle_starts_fresh` |
| Outgoing call | `VOICE` seconds | summed | per started minute beyond allowance (Flex) | `test_flex_plan…` |
| Dropped / zero-duration call | `VOICE` 0 s | counted, 0 minutes | free | personas + scenarios |
| Incoming call at home | `VOICE_MT` | counted | free | rating smoke + scenarios |
| Incoming call roaming EU | `ROAMING_VOICE_MT` zone EU | counted | free (roam like at home) | rating |
| Incoming call roaming Z2/Z3 | `ROAMING_VOICE_MT` zone Z2/Z3 | counted | `voice_mt_per_min` per zone | rating |
| SMS / MMS / international SMS | `SMS` / `MMS` / `SMS_INTL` | counted | SMS beyond allowance; MMS and int'l SMS per item | `test_flex_plan…`, rating |
| International call | `INTL_VOICE` | summed | per started minute | `test_explainer_ranks…` |
| Emergency / toll-free | `EMERGENCY` / `TOLLFREE` | counted | always free | rating |
| Premium / value-added service | `PREMIUM` (provider price in minor units) | summed | passed through as a third-party line | rating |
| Zero-rated data (streaming pass) | `DATA_ZERO_RATED` | counted separately | free, **does not use the allowance or trigger alerts** | personas test |
| EU roaming data / calls | `ROAMING_DATA`/`ROAMING_VOICE` zone EU | count towards domestic allowance | roam like at home | `test_eu_roaming_is_roam_like_at_home…` |
| Z2/Z3 roaming data / calls | zone Z2/Z3 | summed per zone | per MB / per minute per zone | same |
| Add-on purchase | `PLAN_CHANGED` + `addon_code`, only from an order source (`core-orders` once `order-source-token` exists) | allowance grows in the same partition; alert levels re-armed, each new crossing gets a new `alert_id` | add-on price once; unused volume carries across a tariff switch in the month | `test_addon_rearms_alerts…`, `test_add_on_events_only_from_the_dedicated_order_source…`, `test_unused_addon_volume_carries…` |
| Tariff upgrade | order workflow (not a CDR) | real-time profile updated; levels now below are re-armed | pro-rated per segment | `test_tariff_upgrade_rearms_the_levels_now_below` |

## Data quality and delivery semantics

| Scenario | Where | Outcome | Test |
|---|---|---|---|
| Same file re-sent (same batch id) | collector | `duplicate`, nothing republished (batch ids scoped per source) | collector contract |
| Same CDR re-sent in a new file | collector screening | dropped as duplicate (record id remembered 7 days; Bloom filter at scale) | `test_screening_quarantines…` |
| Overlapping batches / re-send while the first is still publishing | collector screening | record ids claimed atomically (`SET NX`): counted once; claims released if publishing fails | `test_overlapping_batches_count_a_record_once…` |
| Duplicate inside one file | collector screening | dropped | same |
| Malformed record (unknown type, missing zone, negative quantity) | collector screening | **quarantined individually** with the validation reason; rest of the file accepted | same |
| Unknown add-on code / unknown roaming zone / quantity > 10¹³ | collector screening | quarantined (they used to crash-loop the aggregator or stop the bill run) | `test_unknown_addon_zone_and_huge_quantity_are_quarantined` |
| Add-on record from a network source | collector | quarantined ("add-on events are not accepted from this source") | `test_add_on_events_only_from…` |
| Message that can never apply | aggregator | pushed to `dlq:{p}`, partition continues | — |
| Future-dated record (clock skew > 10 min) | collector screening | quarantined | same |
| Partial publish then retry | collector + aggregator | deterministic message ids → re-published chunk dropped | `test_republished_message…` |
| Consumer crash and replay | aggregator | offset stored with the state → replay is a no-op | `test_replayed_offset…` |
| Out-of-order arrival | aggregator | totals are sums; cycle by event time | scenarios |
| Late CDR after the bill was issued | bill run | issued bill unchanged; **adjustment line on the next bill**, priced on that month's tariff; the last 3 billed months are checked, each applied once | `test_late_cdrs_after_bill_issue…`, `test_usage_arriving_two_months_late…` |
| Unknown subscriber | aggregator | counted, no alert (no profile) | `test_unknown_subscriber…` |
| Allowance crossed (50/80/100%) | aggregator Lua | exactly one alert per level per cycle | `test_crossing_80…` |
| 100% on a throttle plan | notifier | "speed reduced" notification (network policy hook) | notifier render |
