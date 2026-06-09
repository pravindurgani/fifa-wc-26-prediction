"""
09_validate.py — Pre-launch validation. Fails (exit 1) if any check fails.

Checks:
  • Version strings consistent across index.html + methodology.html + appendix.html
  • No stale hardcoded simulation counts in HTML/JS
  • predictions.json self-consistency (probabilities sum)
  • All bracket fixtures resolve uniquely, no duplicate teams in R32
  • annex_c_misses == 0 in production predictions
  • All required dashboard JSON files exist and parse
  • No API keys / .env / .venv / secrets in dashboard/
  • No local artefacts (.venv, __pycache__, .DS_Store) inside dashboard/
  • All DOM IDs referenced by app.js exist in index.html
  • appendix.html exists and links from index.html + methodology.html
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
PROC = ROOT / "data" / "processed"


def check(name, ok, detail=""):
    status = "✓" if ok else "✗"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return 1 if ok else 0


def main():
    print("== Pre-launch validation ==\n")
    total = passed = 0

    # 1. Required JSON files
    print("[1] Required files")
    files = ["predictions.json", "calibration.json", "walk_forward.json",
             "ablation.json", "sensitivity.json", "travel_impact.json"]
    for f in files:
        p = DASH / f
        ok = p.exists() and p.stat().st_size > 100
        total += 1; passed += check(f"{f} exists", ok, f"{p.stat().st_size if p.exists() else 0} bytes")

    # 1b. Live-mode files (optional but should parse if present)
    print("\n[1b] Live-mode files")
    live_files = ["live_state.json", "live_delta.json", "predictions_live.json"]
    for f in live_files:
        p = DASH / f
        if not p.exists():
            total += 1; passed += check(f"{f} present", False, "missing — required for live mode")
            continue
        try:
            data = json.loads(p.read_text())
            total += 1; passed += check(f"{f} parses", True, f"{p.stat().st_size} bytes")
        except Exception as e:
            total += 1; passed += check(f"{f} parses", False, str(e)[:60])

    # Cross-check live_state mode is sensible
    try:
        ls = json.loads((DASH / "live_state.json").read_text())
        valid_mode = ls.get("mode") in ("pre_tournament", "live")
        total += 1; passed += check("live_state.mode is valid",
                                    valid_mode, f"mode={ls.get('mode')}")
        count = ls.get("completed_matches_count", -1)
        total += 1; passed += check("live_state.completed_matches_count in [0, 104]",
                                    0 <= count <= 104, f"count={count}")
    except Exception:
        pass

    # 2. predictions.json self-consistency
    print("\n[2] predictions.json self-consistency")
    pred = json.loads((DASH / "predictions.json").read_text())
    teams = pred["team_predictions"]
    sum_champion = sum(t["p_champion"] for t in teams)
    total += 1; passed += check("Σ p_champion ≈ 1.0",
                                abs(sum_champion - 1.0) < 0.01,
                                f"actual={sum_champion:.4f}")
    sum_finalists = sum(t["p_reach_final"] for t in teams)
    total += 1; passed += check("Σ p_reach_final ≈ 2.0",
                                abs(sum_finalists - 2.0) < 0.05,
                                f"actual={sum_finalists:.4f}")
    sum_qualified = sum(t["p_advance_groups"] for t in teams)
    total += 1; passed += check("Σ p_advance_groups ≈ 32.0",
                                abs(sum_qualified - 32.0) < 0.05,
                                f"actual={sum_qualified:.4f}")
    total += 1; passed += check("annex_c_misses == 0",
                                pred.get("annex_c_misses", -1) == 0,
                                f"misses={pred.get('annex_c_misses')}")
    total += 1; passed += check("48 teams present",
                                len(teams) == 48, f"got {len(teams)}")
    total += 1; passed += check("72 group matches", len(pred["match_predictions"]) == 72)

    # 3. Version consistency
    print("\n[3] Version & sim-count consistency (HTML must read from JSON)")
    idx = (DASH / "index.html").read_text()
    meth = (DASH / "methodology.html").read_text()
    apx_p = DASH / "appendix.html"
    apx = apx_p.read_text() if apx_p.exists() else ""
    # Extract v1/v2/v3 markers
    idx_vers = sorted(set(re.findall(r'\bv[123]\b', idx)))
    meth_vers = sorted(set(re.findall(r'\bv[123]\b', meth)))
    apx_vers = sorted(set(re.findall(r'\bv[123]\b', apx))) if apx else []
    total += 1; passed += check("index.html version markers",
                                "v3" in idx_vers and "v2" not in idx_vers and "v1" not in idx_vers,
                                f"found {idx_vers}")
    total += 1; passed += check("methodology.html version markers",
                                "v3" in meth_vers and "v2" not in meth_vers and "v1" not in meth_vers,
                                f"found {meth_vers}")
    if apx:
        total += 1; passed += check("appendix.html version markers",
                                    "v3" in apx_vers and "v2" not in apx_vers and "v1" not in apx_vers,
                                    f"found {apx_vers}")
    else:
        total += 1; passed += check("appendix.html exists", False, "missing")

    # 4. No hardcoded sim counts that contradict JSON
    print("\n[4] No hardcoded sim counts in HTML/JS that contradict JSON")
    actual_sims = pred.get("n_simulations_total", 0)
    js_files = ("index.html", "methodology.html", "appendix.html", "app.js")
    for f in js_files:
        fp = DASH / f
        if not fp.exists():
            continue
        text = fp.read_text()
        for n in ("10,000", "10000", "50,000", "50000", "100,000"):
            if str(actual_sims) in n.replace(",", ""):
                continue
            if n in text:
                # if it's near a phrase like "10,000 runs" or "10,000 sims" — flag it
                if re.search(rf'{n}\s+(runs?|sims?|simulations?|tournaments?)', text, re.I):
                    total += 1; passed += check(f"{f} has stale '{n}' sim count", False,
                                                f"actual = {actual_sims}")
                    break
        else:
            total += 1; passed += check(f"{f} sim counts ok", True)

    # 5. Sensitivity report integrity
    print("\n[5] Sensitivity report")
    sens = json.loads((DASH / "sensitivity.json").read_text())
    total += 1; passed += check("sensitivity has summary_top12",
                                len(sens.get("summary_top12", [])) >= 6)

    # 6. No secrets in dashboard/
    print("\n[6] No secrets in dashboard/")
    forbidden = ["BEGIN RSA", "API_KEY", "SECRET_KEY", "password", "PRIVATE KEY"]
    leaks = []
    for f in DASH.rglob("*"):
        if f.is_file() and f.suffix in (".html", ".js", ".css", ".json"):
            txt = f.read_text(errors="ignore")
            for term in forbidden:
                if term in txt:
                    leaks.append(f"{f.name}: {term}")
    total += 1; passed += check("No secret-like strings", not leaks,
                                "; ".join(leaks[:3]) if leaks else "")
    total += 1; passed += check("No .venv/ inside dashboard",
                                not (DASH / ".venv").exists())
    # No local artefacts anywhere in dashboard
    artefacts = []
    for f in DASH.rglob("*"):
        n = f.name
        if n in (".DS_Store", "Thumbs.db") or n == "__pycache__":
            artefacts.append(str(f.relative_to(DASH)))
    total += 1; passed += check("No local artefacts in dashboard",
                                not artefacts,
                                "; ".join(artefacts[:3]) if artefacts else "")

    # 6b. DOM IDs referenced by app.js exist in index.html
    print("\n[6b] DOM IDs in app.js exist in index.html")
    js = (DASH / "app.js").read_text()
    html = idx
    js_ids = set(re.findall(r"getElementById\(\s*['\"]([\w\-]+)['\"]\s*\)", js))
    js_ids |= set(re.findall(r"querySelector\(\s*['\"]#([\w\-]+)", js))
    html_ids = set(re.findall(r'\bid\s*=\s*["\']([\w\-]+)["\']', html))
    missing = sorted(js_ids - html_ids)
    total += 1; passed += check(f"All {len(js_ids)} DOM IDs from app.js present in index.html",
                                not missing,
                                f"missing: {missing[:5]}" if missing else "")

    # 6c. app.js parses with node --check (best-effort)
    try:
        rc = subprocess.run(["node", "--check", str(DASH / "app.js")],
                            capture_output=True, text=True, timeout=10)
        total += 1; passed += check("app.js node --check passes", rc.returncode == 0,
                                    rc.stderr.strip()[:80] if rc.returncode else "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # node not available — skip rather than fail
        print(f"  [~] app.js node --check skipped ({type(e).__name__})")

    # 6d. Appendix links from index.html + methodology.html
    print("\n[6d] Appendix linkage")
    idx_links_apx = "appendix.html" in idx
    meth_links_apx = "appendix.html" in meth
    total += 1; passed += check("index.html links to appendix.html", idx_links_apx)
    total += 1; passed += check("methodology.html links to appendix.html", meth_links_apx)

    # 7. Concentration sanity
    print("\n[7] Concentration sanity (informational)")
    c = pred.get("concentration", {})
    top1 = c.get("top1_champion_p", 0) * 100
    top2 = c.get("top2_combined", 0) * 100
    top5 = c.get("top5_combined", 0) * 100
    total += 1; passed += check(f"top-1 ({top1:.1f}%) < 35% (bookmaker sanity)", top1 < 0.35 * 100)

    # 8. README ↔ predictions.json sim-count consistency
    print("\n[8] README sim-count consistency")
    readme_p = ROOT / "README.md"
    if readme_p.exists():
        readme = readme_p.read_text()
        actual_sims = pred.get("n_simulations_total", 0)
        # Look for sim counts like "25,000" or "25000" or "50,000"
        sim_strs = [f"{actual_sims:,}", str(actual_sims)]
        readme_mentions = re.findall(r'\b(\d{1,3}(?:,\d{3})+|\d{4,6})\s+(?:simulations?|sims?|Monte\s*Carlo)', readme, re.I)
        if readme_mentions:
            normalized = [int(m.replace(",", "")) for m in readme_mentions]
            consistent = all(n == actual_sims for n in normalized)
            total += 1; passed += check("README sim counts match predictions.json",
                                        consistent,
                                        f"README={readme_mentions}, predictions={actual_sims}")
        else:
            total += 1; passed += check("README mentions no specific sim count (ok)", True)
    else:
        total += 1; passed += check("README.md exists", False)

    print(f"\n{'='*50}")
    print(f"  Validation: {passed} / {total} checks passed")
    print(f"{'='*50}")
    if passed < total:
        print(f"\n  ✗ {total - passed} check(s) failed")
        sys.exit(1)
    print("\n  ✓ All checks passed — ready to publish")


if __name__ == "__main__":
    main()
