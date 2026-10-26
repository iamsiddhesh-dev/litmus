# Bazaar benchmark

Litmus is scored against a fixed **Bazaar** demo app with known bugs. The platform
does not hardcode Bazaar in the engine—only `profiles/`, `fixtures/`, and
`benchmark/` in this repo point at the fixture.

## Expected findings

| ID | Type | Expected after verify×3 |
|---|---|---|
| B1 | Crash (NPE on second add-to-cart) | Confirmed |
| B2 | HTTP 5xx on checkout | Confirmed |
| B3 | Dead tap (coupon button) | Confirmed |
| B4 | Broken UI (title/price overlap) | Confirmed |
| B5 | Random express-checkout crash | Flaky or not observed (N/A) |

## Scoring

- **Recall:** confirmed matches for B1–B4 ÷ 4  
- **B5:** if seen in a crawl, verdict must be **flaky** (not sold as stable)  
- **False positives:** confirmed findings that are not B1–B4  

Run:

```bash
mqa bench --fixture bazaar --runs 3
```

Results are written under `benchmark/results/`.

## Fixture repo

Bazaar source, contract, and APK releases live in the **bazaar** GitHub repository
(link in README once published). Mock API must be running for network scenarios.
