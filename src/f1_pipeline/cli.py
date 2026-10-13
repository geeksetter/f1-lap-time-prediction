import argparse
import json
import logging
import os
from pathlib import Path

from .api import OpenF1Client
from .collect import collect_year
from .config import ROOT, load_config
from .db import connect, counts, init_db
from .prepare import prepare_dataset
from .quality import write_report


def main():
    parser = argparse.ArgumentParser(
        description="OpenF1 -> PostgreSQL -> ordinary laps -> OOP regression -> evaluation"
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    collect = sub.add_parser("collect")
    collect.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025])
    collect.add_argument("--country", default="Spain")
    collect.add_argument("--circuit", default="Catalunya")
    collect.add_argument(
        "--verify-repeat",
        action="store_true",
        help="Fetch twice and compare source-table counts and hashes",
    )
    report = sub.add_parser("quality")
    report.add_argument("--output", default=str(ROOT / "reports"))
    sub.add_parser("counts")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025])
    prepare.add_argument("--circuit", default="Catalunya")
    prepare.add_argument("--output", default=str(ROOT / "reports"))
    prepare.add_argument("--verify-repeat", action="store_true")
    eda = sub.add_parser("eda")
    eda.add_argument("--output", default=str(ROOT / "reports"))
    train = sub.add_parser("train")
    train.add_argument("--output", default=str(ROOT / "artifacts/stage3"))
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--artifacts", default=str(ROOT / "artifacts/stage3"))
    evaluate.add_argument("--output", default=str(ROOT / "reports/modeling"))
    predict = sub.add_parser("predict")
    predict.add_argument("--artifacts", default=str(ROOT / "artifacts/stage3"))
    predict.add_argument("--session", type=int, required=True)
    predict.add_argument("--driver", type=int, required=True)
    predict.add_argument("--lap", type=int, required=True)
    args = parser.parse_args()
    load_config()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "init-db":
        init_db()
        print("Schema f1 initialized")
    elif args.command == "counts":
        with connect() as conn:
            print(json.dumps(counts(conn), indent=2))
    elif args.command == "quality":
        r = write_report(args.output)
        print(
            json.dumps(
                {"counts": r["counts"], "issues": r["issues"]},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        if any(i["severity"] == "critical" for i in r["issues"]):
            raise SystemExit(2)
    elif args.command == "prepare":
        first = prepare_dataset(
            output=args.output, years=args.years, circuit=args.circuit
        )
        if args.verify_repeat:
            second = prepare_dataset(
                output=args.output, years=args.years, circuit=args.circuit
            )
            proof = {
                "first_run": first["run_id"],
                "repeat_run": second["run_id"],
                "input_hash_equal": first["input_sha256"] == second["input_sha256"],
                "output_hash_equal": first["output_sha256"] == second["output_sha256"],
                "counts_equal": first["total"] == second["total"],
                "before": first["total"],
                "after": second["total"],
            }
            Path(args.output).joinpath("repeat_preparation.json").write_text(
                json.dumps(proof, indent=2) + "\n"
            )
            if not all(
                proof[k]
                for k in ("input_hash_equal", "output_hash_equal", "counts_equal")
            ):
                raise RuntimeError("Preparation is not repeatable")
        print(
            json.dumps({"total": first["total"], "checks": first["checks"]}, indent=2)
        )
    elif args.command == "eda":
        from .eda import write_eda

        result = write_eda(args.output)
        print(
            json.dumps({"rows": result["rows"], "figures": result["figures"]}, indent=2)
        )
    elif args.command == "train":
        from .experiment import train_experiment

        result = train_experiment(args.output)
        print(
            json.dumps(
                {
                    "selected_model": result["selected_model"],
                    "split": result["split"],
                    "validation_metrics": result["validation_metrics"],
                },
                indent=2,
            )
        )
    elif args.command == "evaluate":
        from .experiment import evaluate_experiment

        result = evaluate_experiment(args.artifacts, args.output)
        print(
            json.dumps(
                {
                    "selected_model": result["selected_model"],
                    "test_metrics": result["test_metrics"],
                },
                indent=2,
            )
        )
    elif args.command == "predict":
        from .experiment import predict_lap

        print(
            json.dumps(
                predict_lap(args.artifacts, args.session, args.driver, args.lap),
                indent=2,
            )
        )
    elif args.command == "collect":
        if any(y < 2023 for y in args.years):
            parser.error("OpenF1 history starts in 2023")
        client = OpenF1Client(
            base_url=os.getenv("OPENF1_BASE_URL", "https://api.openf1.org/v1"),
            timeout=float(os.getenv("OPENF1_TIMEOUT", "60")),
            attempts=int(os.getenv("OPENF1_ATTEMPTS", "4")),
            min_interval=float(os.getenv("OPENF1_MIN_INTERVAL", "2.1")),
        )
        try:
            runs = [
                collect_year(client, y, args.country, args.circuit) for y in args.years
            ]
            with connect() as conn:
                before = counts(conn)
            if args.verify_repeat:
                repeated = [
                    collect_year(client, y, args.country, args.circuit)
                    for y in args.years
                ]
                with connect() as conn:
                    after = counts(conn)
                    hashes = conn.execute(
                        """SELECT a.year,b.endpoint,b.content_sha256 FROM f1.ingestion_runs a
                        JOIN f1.api_batches b USING(run_id) WHERE a.run_id=ANY(%s::uuid[])
                        ORDER BY a.year,b.endpoint""",
                        (runs,),
                    ).fetchall()
                    hashes2 = conn.execute(
                        """SELECT a.year,b.endpoint,b.content_sha256 FROM f1.ingestion_runs a
                        JOIN f1.api_batches b USING(run_id) WHERE a.run_id=ANY(%s::uuid[])
                        ORDER BY a.year,b.endpoint""",
                        (repeated,),
                    ).fetchall()
                proof = {
                    "first_runs": runs,
                    "repeat_runs": repeated,
                    "before": before,
                    "after": after,
                    "counts_equal": before == after,
                    "response_hashes_equal": hashes == hashes2,
                }
                (ROOT / "reports").mkdir(exist_ok=True)
                (ROOT / "reports/repeat_load.json").write_text(
                    json.dumps(proof, indent=2) + "\n"
                )
                print(json.dumps(proof, indent=2))
                if before != after:
                    raise RuntimeError(
                        "Source table counts changed; inspect upstream changes"
                    )
            else:
                print(json.dumps({"run_ids": runs, "counts": before}, indent=2))
        finally:
            client.close()


if __name__ == "__main__":
    main()
