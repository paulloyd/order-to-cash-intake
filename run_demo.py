#!/usr/bin/env python3
"""Build the demo end to end: seed master data -> generate messy orders -> run them through the pipeline ->
simulate the human/ERP side -> print and save measured results.

    python run_demo.py            # offline, deterministic (rules extractor, mock BC/HubSpot/Magento)
"""
import json, random, sys
from datetime import datetime, timedelta
from pathlib import Path

from otc import config, db as D, seed, synth, pipeline, metrics


def main(db_path=None, quiet=False):
    path = Path(db_path) if db_path else config.DB_PATH
    path.unlink(missing_ok=True)
    db = D.connect(path)
    seed.build(db)
    truth = synth.generate(db, config.INBOX_DIR)
    by_file = {t["file"]: t for t in truth}

    # 1) email orders (.eml with real PDF attachments), in arrival order
    for f in sorted(config.INBOX_DIR.glob("*.eml")):
        by_file[f.name]["case_id"] = pipeline.ingest_eml_file(db, f)
    # 2) portal form orders
    forms = json.loads((config.DATA_DIR / "form_orders.json").read_text())
    for i, fo in enumerate(forms, 1):
        by_file[f"form_{i}"]["case_id"] = pipeline.ingest_form(db, fo["sender"], fo["po"], fo["date"], fo["ship"], fo["lines"], datetime.fromisoformat(fo["t"]))
    # 3) Magento poll (same pipeline, structured input)
    pipeline.pull_magento(db)
    for t in truth:
        if t["channel"] == "magento":
            t["case_id"] = D.one(db, "SELECT id FROM messages WHERE subject=?", (f"Magento order {t['file']}",))["id"]

    # 4) simulate the people: reps work the queue using the drafted emails / suggestions; customers answer.
    #    The *resolutions* below are simulated (they use ground truth) - only used to show the assisted path end to end.
    rng = random.Random(3)
    metrics.simulate_resolutions(db, truth, rng)
    # 5) simulate the warehouse shipping ~60% of confirmed orders, then let the shipment sync pick them up
    bc = pipeline.get_bc(db)
    for o in D.q(db, "SELECT bc_order_no FROM orders ORDER BY id"):
        if rng.random() < 0.6:
            bc.ship(o["bc_order_no"], f"1Z{rng.randint(10**9, 10**10 - 1)}")
    pipeline.sync_shipments(db, now=datetime(2026, 10, 19, 9, 0))
    db.commit()

    (config.DATA_DIR / "truth.json").write_text(json.dumps(truth, indent=1))
    k, ev = metrics.kpis(db), metrics.evaluate(db, truth)
    config.REPORT_DIR.mkdir(exist_ok=True)
    (config.REPORT_DIR / "kpis.json").write_text(json.dumps(k, indent=2))
    (config.REPORT_DIR / "evaluation.json").write_text(json.dumps(ev, indent=2))
    if not quiet:
        print(metrics.render(k, ev))
    return db, k, ev


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
